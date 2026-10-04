"""
Email 8D reports and machine-down alerts.

Three transports (picked in Settings, see services/mail_transport.py):

  snowflake - SYSTEM$SEND_EMAIL. HTML only, so the PDF is uploaded to an
              internal stage with server-side encryption and the email carries a
              pre-signed download link (valid up to 7 days). Recipients must be
              users of this account with verified emails. Needs an EMAIL
              notification integration (sql/06_report_email.sql).
  smtp, sendgrid - any address, PDF attached. Need an external access
              integration and a secret inside Snowflake (sql/08_external_email.sql).

Saved recipient lists live in the operations store (RECIPIENT_LISTS); every
send is recorded in EMAIL_LOG, sent or failed.
"""

import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from html import escape

from snowflake_app.services import mail_transport as T, ops_store
from snowflake_app.services import report_export as X
from snowflake_app.services.llm import SnowflakePool

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_LINK_DAYS = 7


class EmailError(RuntimeError):
    pass


def _ident(v: str, what: str) -> str:
    v = (v or "").strip()
    if not _IDENT.match(v):
        raise EmailError(f"{what} '{v}' is not a valid Snowflake identifier.")
    return v.upper()


def parse_recipients(text) -> list[str]:
    items = text if isinstance(text, (list, tuple)) else re.split(r"[,;\s]+", text or "")
    items = [str(x).strip() for x in items if str(x).strip()]
    bad = [x for x in items if not _EMAIL.match(x)]
    if bad:
        raise EmailError(f"Not a valid email address: {', '.join(bad)}")
    if not items:
        raise EmailError("Add at least one recipient.")
    return list(dict.fromkeys(items))


def _cfg(settings: dict) -> tuple[dict, str, str, int]:
    em = settings.get("email") or {}
    return (settings["providers"]["snowflake"], _ident(em.get("integration"), "Integration"),
            _ident(em.get("stage"), "Stage"), max(1, min(int(em.get("link_days") or MAX_LINK_DAYS), MAX_LINK_DAYS)))


_status_cache: dict = {}


def check(settings: dict, force: bool = False) -> dict:
    """{ok, integration, stage, issues[]} without sending anything. Cached for 60 s."""
    try:
        sf, integ, stage, _ = _cfg(settings)
    except EmailError as e:
        return {"ok": False, "integration": False, "stage": False, "issues": [str(e)]}
    key = (integ, stage, sf.get("mode"), sf.get("connection_name"))
    hit = _status_cache.get(key)
    if hit and not force and time.time() - hit[0] < 60:
        return hit[1]
    res = {"ok": False, "integration": False, "stage": False, "issues": [], "integration_name": integ, "stage_name": stage}
    try:
        rows = SnowflakePool.query(sf, "SHOW NOTIFICATION INTEGRATIONS LIKE %s", (integ.split(".")[-1],))
        if not rows:
            res["issues"].append(f"Notification integration {integ} was not found, or this role cannot use it.")
        else:
            row = [str(x).upper() for x in rows[0]]
            res["integration"] = "EMAIL" in row and "TRUE" in row
            if not res["integration"]:
                res["issues"].append(f"{integ} exists but is not an enabled EMAIL integration.")
    except Exception as e:
        res["issues"].append(f"Could not check the integration: {str(e).splitlines()[0][:200]}")
    try:
        SnowflakePool.query(sf, f"DESC STAGE {stage}")
        res["stage"] = True
    except Exception:
        res["issues"].append(f"Stage {stage} does not exist yet; it is created on the first send if the role allows it.")
    res["ok"] = res["integration"]
    _status_cache[key] = (time.time(), res)
    return res


def _ensure_stage(sf: dict, stage: str):
    SnowflakePool.query(sf, f"CREATE STAGE IF NOT EXISTS {stage} ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') "
                            "DIRECTORY = (ENABLE = TRUE) COMMENT = '8D Intelligence: emailed report PDFs'")


def upload_pdf(settings: dict, snap: dict) -> dict:
    """Upload the report PDF; returns {path, url, expires}."""
    sf, _, stage, days = _cfg(settings)
    _ensure_stage(sf, stage)
    pdf = X.to_pdf(snap)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{X.file_stem(snap)}.pdf"
    folder = f"{snap['id']}/{stamp}"
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, name)
        with open(local, "wb") as f:
            f.write(pdf)
        SnowflakePool.query(sf, f"PUT 'file://{local}' @{stage}/{folder} AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
    path = f"{folder}/{name}"
    secs = days * 86400
    rows = SnowflakePool.query(sf, f"SELECT GET_PRESIGNED_URL(@{stage}, %s, %s)", (path, secs))
    expires = (datetime.now() + timedelta(seconds=secs)).strftime("%b %-d, %Y %I:%M %p")
    return {"path": path, "url": rows[0][0], "expires": expires, "bytes": len(pdf)}


def send(settings: dict, snap: dict, recipients: str, subject: str, note: str = "", sender: str = "",
         attach_pdf: bool = True, include_full: bool = False) -> dict:
    sf, integ, stage, _ = _cfg(settings)
    to = parse_recipients(recipients)
    subject = (subject or "").strip() or f"8D Report {snap['id']}"
    up = upload_pdf(settings, snap) if attach_pdf else {"url": None, "expires": None}
    body = X.email_html(snap, up["url"], up["expires"], note=note, sender=sender, include_full=include_full)
    try:
        SnowflakePool.query(sf, "CALL SYSTEM$SEND_EMAIL(%s, %s, %s, %s, 'text/html')",
                            (integ, ", ".join(to), subject[:250], body))
    except Exception as e:
        msg = str(e).splitlines()[0][:300]
        if "verif" in msg.lower() or "recipient" in msg.lower():
            msg += " Recipients must be users of this Snowflake account with a verified email address."
        raise EmailError(msg) from e
    return {"to": to, "subject": subject, "pdf": up.get("path"), "url": up["url"], "expires": up["expires"],
            "stage": stage, "sent_at": datetime.now().isoformat(timespec="seconds")}


_me: dict = {}


def current_user_email(settings: dict) -> str:
    """Email of the signed-in Snowflake user (DESC USER on oneself needs no extra grants)."""
    sf = settings["providers"]["snowflake"]
    key = (sf.get("mode"), sf.get("connection_name"), sf.get("user"))
    if key in _me:
        return _me[key]
    email = ""
    try:
        user = SnowflakePool.query(sf, "SELECT CURRENT_USER()")[0][0]
        for row in SnowflakePool.query(sf, "DESC USER IDENTIFIER(%s)", (f'"{user}"',)):
            if str(row[0]).upper() == "EMAIL" and row[1] and str(row[1]).lower() != "null":
                email = str(row[1])
                break
    except Exception:
        pass
    _me[key] = email
    return email


# ---------------------------------------------------------------------------
# One entry point for every transport
# ---------------------------------------------------------------------------

def transport(settings: dict) -> str:
    return (settings.get("email") or {}).get("transport") or "snowflake"


def ready(settings: dict, force: bool = False) -> dict:
    """{ok, transport, label, issues, any_address} for the configured transport."""
    t = transport(settings)
    if t == "snowflake":
        c = check(settings, force=force)
        return {**c, "transport": t, "label": T.TRANSPORTS[t], "any_address": False}
    c = T.check(settings, t)
    return {**c, "label": T.TRANSPORTS[t], "any_address": True}


def _log(rec: dict):
    ops_store.store().put("email_log", {"email_id": uuid.uuid4().hex[:12], "sent_at": ops_store.now_iso(), **rec})


def deliver(settings: dict, snap: dict, recipients, subject: str = "", note: str = "", sender: str = "",
            include_full: bool = False, list_name: str | None = None, reason: str = "manual") -> dict:
    """Send a report with the configured transport and record it in the email log."""
    t = transport(settings)
    to = parse_recipients(recipients)
    subject = (subject or "").strip() or f"8D Report {snap['id']} · {X.build(snap)['title']}"
    base = {"run_id": snap["id"], "list_name": list_name, "recipients": to, "subject": subject[:250], "transport": t,
            "reason": reason}
    try:
        if t == "snowflake":
            res = send(settings, snap, ", ".join(to), subject, note, sender, attach_pdf=True, include_full=include_full)
        else:
            stem = X.file_stem(snap)
            files = [(f"{stem}.pdf", X.to_pdf(snap), "application/pdf")]
            if (settings.get("email") or {}).get("attach_docx"):
                files.append((f"{stem}.docx", X.to_docx(snap),
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
            body = X.email_html(snap, None, None, note=note, sender=sender, include_full=include_full, attached=True)
            r = T.send(settings, t, to, subject[:250], body, text=X.build(snap)["subtitle"], attachments=files)
            res = {"to": r["accepted"], "refused": r["refused"], "subject": subject, "pdf": None, "url": None, "expires": None,
                   "attachments": [f[0] for f in files], "sent_at": datetime.now().isoformat(timespec="seconds")}
    except Exception as e:
        _log({**base, "status": "failed", "error": str(e)[:500]})
        raise EmailError(str(e)) from e
    _log({**base, "status": "sent", "error": None})
    return {**res, "transport": t, "list_name": list_name}


def send_html(settings: dict, recipients, subject: str, html: str, reason: str, list_name: str | None = None,
              run_id: str | None = None) -> dict:
    """Plain notification (no report attached) with the configured transport."""
    t = transport(settings)
    to = parse_recipients(recipients)
    base = {"run_id": run_id, "list_name": list_name, "recipients": to, "subject": subject[:250], "transport": t, "reason": reason}
    try:
        if t == "snowflake":
            sf, integ, _, _ = _cfg(settings)
            SnowflakePool.query(sf, "CALL SYSTEM$SEND_EMAIL(%s, %s, %s, %s, 'text/html')", (integ, ", ".join(to), subject[:250], html))
        else:
            T.send(settings, t, to, subject[:250], html)
    except Exception as e:
        _log({**base, "status": "failed", "error": str(e)[:500]})
        raise EmailError(str(e).splitlines()[0][:300]) from e
    _log({**base, "status": "sent", "error": None})
    return {"to": to, "transport": t}


def alert_html(wo: dict, event: dict, impact: dict | None = None) -> str:
    """Machine-down notification body; `impact` (services/impact_ops.for_machine) adds the orders at risk."""
    rows = [("Machine", f"{wo['machine_id']} · {wo.get('machine_name') or ''}"), ("Line", wo.get("line") or ""),
            ("What happened", event.get("message") or wo.get("reason") or ""), ("Work order", f"{wo['wo_id']} · {wo.get('priority_label', '')}"),
            ("Likely component", wo.get("component") or ""), ("Estimate", f"{wo.get('est_hours', 0):g} h · ${wo.get('est_cost_usd', 0):,.0f}"),
            ("Technician", wo.get("technician_name") or "Unassigned")]
    try:
        from snowflake_app.services import parts_ops
        parts = ", ".join(f"{p['part_name']} ({'held' if p['covered'] else 'short, ' + str(p['lead_time_days']) + ' d lead time'})"
                          for p in parts_ops.wo_parts(wo)) or "No matching spares listed"
    except Exception:
        parts = ", ".join(p["part_name"] for p in wo.get("parts") or []) or "No matching spares listed"
    rows.append(("Spare parts", parts))
    sc = (impact or {}).get("scenario") or {}
    if sc:
        per_h = sc["cost_per_h"]["total_h"]
        rows.append(("Cost of this stop", f"${per_h:,.0f} per hour down · about ${sc['total_cost']:,.0f} in total"))
        rows.append(("Customer orders", f"{sc['late_count']} will be late (${sc['value_at_risk']:,.0f}, "
                                        f"${sc['penalty_usd']:,.0f} penalties) · {sc['tight_count']} tight"
                     if sc["late_count"] or sc["tight_count"] else "None late if the repair takes the estimated time"))
        alt = next((a for a in (impact or {}).get("alternates") or [] if a["covers"]), None)
        if alt and sc["late_count"]:
            rows.append(("Move work to", f"{alt['machine_id']} {alt.get('name') or ''} ({alt['spare_hours_7d']:g} h free this week)"))
    late = "".join(f'<tr><td style="padding:4px 8px;font-size:12px">{escape(o["order_id"])}</td>'
                   f'<td style="padding:4px 8px;font-size:12px">{escape(str(o.get("customer_name") or ""))}</td>'
                   f'<td style="padding:4px 8px;font-size:12px">{escape(str(o["due_date"]))}</td>'
                   f'<td style="padding:4px 8px;font-size:12px;text-align:right">${float(o["order_value_usd"]):,.0f}</td></tr>'
                   for o in (sc.get("late") or [])[:6])
    late_html = (f'<p style="margin:16px 0 6px;font-size:13px;font-weight:700;color:#1B1816">Orders at risk</p>'
                 f'<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #E5E2DE">'
                 f'<tr style="background:#F5F4F2"><th align="left" style="padding:4px 8px;font-size:11px">Order</th>'
                 f'<th align="left" style="padding:4px 8px;font-size:11px">Customer</th><th align="left" style="padding:4px 8px;font-size:11px">Due</th>'
                 f'<th align="right" style="padding:4px 8px;font-size:11px">Value</th></tr>{late}</table>') if late else ""
    steps = "".join(f"<li style='margin:0 0 4px'>{escape(s)}</li>" for s in wo.get("checklist") or [])
    table = "".join(f'<tr><td style="padding:5px 0;font-size:12px;color:#78716B;width:140px;vertical-align:top">{escape(k)}</td>'
                    f'<td style="padding:5px 0;font-size:13px;color:#1B1816;font-weight:600">{escape(str(v))}</td></tr>' for k, v in rows)
    return (f'<!doctype html><html><body style="margin:0;background:#F3F2F0;font-family:Segoe UI,Helvetica,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 12px"><tr><td align="center">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:12px;overflow:hidden">'
            f'<tr><td style="background:#F26B1D;padding:18px 26px"><p style="margin:0;font-size:11px;font-weight:700;letter-spacing:.08em;color:#fff">MACHINE DOWN</p>'
            f'<p style="margin:6px 0 0;font-size:20px;font-weight:700;color:#fff">{escape(wo["machine_id"])} stopped and needs maintenance</p></td></tr>'
            f'<tr><td style="padding:20px 26px"><table role="presentation" width="100%">{table}</table>{late_html}'
            f'<p style="margin:16px 0 6px;font-size:13px;font-weight:700;color:#1B1816">Next steps</p><ol style="margin:0;padding-left:18px;font-size:13px;color:#3F3A36">{steps}</ol>'
            f'</td></tr><tr><td style="padding:14px 26px;background:#F5F4F2;font-size:11.5px;color:#78716B">Sent by 8D Intelligence live monitoring. '
            f'The machine stays offline until the repair is completed in the app.</td></tr></table></td></tr></table></body></html>')


def log(run_id: str | None = None, limit: int = 50) -> list[dict]:
    rows = ops_store.store().all("email_log")
    if run_id:
        rows = [r for r in rows if r.get("run_id") == run_id]
    return sorted(rows, key=lambda r: r.get("sent_at") or "", reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Saved recipient lists
# ---------------------------------------------------------------------------

def lists() -> list[dict]:
    rows = ops_store.store().all("recipient_lists")
    return sorted(rows, key=lambda r: (not r.get("is_default"), r["name"].lower()))


def default_list() -> dict | None:
    ls = lists()
    return next((x for x in ls if x.get("is_default")), ls[0] if ls else None)


def save_list(name: str, emails, on_report: bool = False, on_machine_down: bool = False, is_default: bool = False,
              old_name: str | None = None, daily_digest: bool = False) -> dict:
    name = (name or "").strip()
    if not name:
        raise EmailError("Give the list a name.")
    addrs = parse_recipients(emails)
    store = ops_store.store()
    if old_name and old_name != name:
        store.delete("recipient_lists", old_name)
    if is_default:
        for other in lists():
            if other["name"] != name and other.get("is_default"):
                store.put("recipient_lists", {**other, "is_default": False})
    rec = {"name": name, "emails": addrs, "on_report": bool(on_report), "on_machine_down": bool(on_machine_down),
           "daily_digest": bool(daily_digest), "is_default": bool(is_default) or not lists(), "updated_at": ops_store.now_iso()}
    store.put("recipient_lists", rec)
    return rec


def delete_list(name: str):
    ops_store.store().delete("recipient_lists", name)
