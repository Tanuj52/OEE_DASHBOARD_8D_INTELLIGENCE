import time
from datetime import datetime

import streamlit as st

from snowflake_app.core import capa as C
from snowflake_app.core.pipeline import report_markdown
from snowflake_app.services import capa_ops, mailer, report_export as X
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/report_view.py"
REPORTS = "app_pages/reports.py"
shell.topbar(PATH)
rm = shell.run_manager()
ss = st.session_state
FINISHED = ("completed", "cancelled", "failed")

rid = st.query_params.get("id") or ss.get("report_open")
snap = rm.get(rid) if rid else None
if not snap or snap["status"] not in FINISHED:
    with K.card("missing"):
        msg = ("This investigation is still running. Its report opens here when the agents finish." if snap
               else "This report could not be found. It may have been created in another session.")
        st.html(f'<div style="text-align:center;padding:36px 10px"><div style="font-weight:600;font-size:15px">'
                f'{"Report not ready yet" if snap else "Report not found"}</div>'
                f'<div class="sx-subtle" style="font-size:12.5px;margin-top:4px">{K.esc(msg)}</div></div>')
        with st.container(horizontal=True, horizontal_alignment="center"):
            st.button("All reports", icon=":material/arrow_back:", type="primary", on_click=shell.goto, args=(REPORTS,))
            if snap:
                st.button("Watch the investigation", icon=":material/account_tree:", on_click=shell.goto,
                          args=("app_pages/investigations.py",))
    st.stop()

ss.report_open = rid
if st.query_params.get("id") != rid:
    st.query_params["id"] = rid
rep = X.build(snap)
t = snap["trigger"]
stem = X.file_stem(snap)
cfg = shell.settings()


def viewer_email() -> str:
    try:
        e = st.user.get("email")
    except Exception:
        e = None
    return e or mailer.current_user_email(cfg)


# ------------------------------------------------------------------ email
def _send_to_list(run_id: str, name: str):
    """One click: send the report to a saved list with the configured transport."""
    lst = next((x for x in mailer.lists() if x["name"] == name), None)
    if not lst:
        shell.notify("That recipient list no longer exists.", icon=":material/error:")
        return
    try:
        res = mailer.deliver(cfg, rm.get(run_id), lst["emails"], sender=viewer_email(), list_name=name, reason="one-click")
        shell.notify(f"Report sent to {name} ({len(res['to'])} recipients)", icon=":material/mark_email_read:")
    except Exception as e:
        shell.notify(f"Email failed: {str(e)[:200]}", icon=":material/error:")


def _queue_email():
    ss.email_req = {"to": ss.get("em_to") or [], "subject": ss.get("em_subject", ""), "note": ss.get("em_note", ""),
                    "full": ss.get("em_full", False), "save": ss.get("em_save_list", False), "name": ss.get("em_list_name", "")}


def _close_email():
    ss.email_open = None
    ss.pop("email_result", None)
    ss.pop("email_error", None)


@st.dialog("Email this report", width="medium", icon=":material/mail:", on_dismiss=_close_email)
def email_dialog(run_id: str):
    chk = mailer.ready(cfg)
    if not chk["ok"]:
        st.error(f"{chk['label']} is not set up yet.", icon=":material/error:")
        for i in chk["issues"]:
            st.caption(i)
        st.button("Open email settings", icon=":material/settings:", on_click=shell.goto, args=("app_pages/settings.py",))
        return
    req = ss.pop("email_req", None)
    if req:
        with st.spinner("Creating the PDF and sending the email…"):
            try:
                if req["save"] and req["to"]:
                    mailer.save_list(req["name"] or "Recipients", req["to"])
                res = mailer.deliver(cfg, rm.get(run_id), req["to"], req["subject"], req["note"], sender=viewer_email(),
                                     include_full=req["full"], list_name=req["name"] if req["save"] else None)
                ss.email_result = {**res, "id": run_id}
                ss.pop("email_error", None)
            except Exception as e:
                ss.email_error = str(e)
    res = ss.get("email_result")
    if res and res.get("id") == run_id:
        st.success(f"Sent to **{', '.join(res['to'])}**", icon=":material/mark_email_read:")
        facts = [("Subject", res["subject"]), ("Sent with", mailer.T.TRANSPORTS.get(res["transport"], res["transport"]))]
        if res.get("attachments"):
            facts.append(("Attached", ", ".join(res["attachments"])))
        if res.get("pdf"):
            facts += [("PDF link", f"valid until {res['expires']}"), ("Stored at", f"@{res['stage']}/{res['pdf']}")]
        if res.get("refused"):
            facts.append(("Refused", ", ".join(res["refused"])))
        st.html('<div class="sx-inset" style="padding:10px 12px;font-size:12.5px;display:grid;gap:6px">' + "".join(
            f'<div class="sx-row"><span class="sx-subtle" style="width:90px;flex:none">{K.esc(k)}</span><span style="overflow-wrap:anywhere">{K.esc(v)}</span></div>'
            for k, v in facts) + "</div>")
        if st.button("Done", type="primary", icon=":material/check:", key="em_done"):
            _close_email()
            st.rerun()
        return
    if ss.get("email_error"):
        st.error(ss.email_error, icon=":material/error:")
    lists = mailer.lists()
    known = sorted({e for x in lists for e in x["emails"]} | {viewer_email()} - {""})
    if "em_to" not in ss:
        d = mailer.default_list()
        ss.em_to = list(d["emails"]) if d else ([viewer_email()] if viewer_email() else [])
    ss.setdefault("em_subject", f"8D Report {run_id} · {rep['title']}")
    with st.form("email_form", border=False):
        st.multiselect("To", sorted(set(known) | set(ss.em_to)), key="em_to", accept_new_options=True,
                       placeholder="Type an address and press Enter")
        st.text_input("Subject", key="em_subject")
        st.text_area("Message", key="em_note", height=90, placeholder="Optional note for the recipients")
        st.checkbox("Include the full report in the email body", value=False, key="em_full")
        c1, c2 = st.columns([1, 2], vertical_alignment="bottom")
        c1.checkbox("Save as a list", key="em_save_list")
        c2.text_input("List name", key="em_list_name", placeholder="e.g. Supplier quality", label_visibility="collapsed")
        if chk["any_address"]:
            st.caption(f"Sent with {chk['label']}: any address, PDF attached.")
        else:
            st.caption("Sent with Snowflake email: recipients must be users of this account with a verified email; the PDF is "
                       "shared as a secure link. Switch to SMTP or SendGrid in Settings to reach anyone.")
        st.form_submit_button("Send email", type="primary", icon=":material/send:", on_click=_queue_email)


def _open_email(run_id: str):
    for k in ("em_to", "em_subject", "em_note", "email_result", "email_error", "email_req", "em_save_list", "em_list_name"):
        ss.pop(k, None)
    ss.email_open = run_id


def email_buttons(run_id: str):
    lists = mailer.lists()
    d = mailer.default_list()
    if d:
        st.button(f"Email · {d['name']}", icon=":material/send:", key="rv_email_list", on_click=_send_to_list, args=(run_id, d["name"]),
                  help=f"Send now to {', '.join(d['emails'])}")
        with st.popover("More", icon=":material/group:", help="Other recipients"):
            for x in lists:
                if x["name"] != d["name"]:
                    st.button(f"Send to {x['name']}", key=f"rv_send_{x['name']}", icon=":material/group:", type="tertiary",
                              on_click=_send_to_list, args=(run_id, x["name"]), help=", ".join(x["emails"]))
            st.button("Custom recipients…", key="rv_email", icon=":material/edit:", type="tertiary", on_click=_open_email, args=(run_id,))
    else:
        st.button("Email", icon=":material/mail:", key="rv_email", on_click=_open_email, args=(run_id,))


# --------------------------------------------------------------------- header
finished = [r for r in rm.list() if r["status"] in FINISHED]
ids = [r["id"] for r in finished]
pos = ids.index(rid) if rid in ids else -1
with st.container(horizontal=True, vertical_alignment="center"):
    st.button("All reports", icon=":material/arrow_back:", type="tertiary", key="rv_back", on_click=shell.goto, args=(REPORTS,))
    st.space("stretch")
    st.button("Newer", icon=":material/chevron_left:", type="tertiary", key="rv_newer", disabled=pos <= 0,
              on_click=shell.open_report, args=(ids[pos - 1] if pos > 0 else rid,))
    st.html(f'<span class="sx-subtle tabular" style="font-size:12px">{pos + 1} of {len(ids)}</span>')
    st.button("Older", icon=":material/chevron_right:", icon_position="right", type="tertiary", key="rv_older",
              disabled=pos < 0 or pos >= len(ids) - 1, on_click=shell.open_report, args=(ids[pos + 1] if 0 <= pos < len(ids) - 1 else rid,))

dec = snap.get("decision") or {}
pills = [K.pill(snap["status"], size="xs")]
if t.get("risk"):
    pills.append(K.pill(t["risk"], f"{t['risk'].capitalize()} risk", "xs"))
if dec.get("escalate"):
    pills.append('<span class="sx-pill solid xs">Full 8D</span>')
log = [x for x in mailer.log(rid) if x.get("status") == "sent"]
with K.card("rv_head", accent=True):
    st.html(f'<div class="sx-row" style="gap:6px;flex-wrap:wrap"><span class="sx-label" style="margin-right:4px">8D report</span>'
            f'<span class="sx-mono sx-subtle" style="font-size:12px">{K.esc(rid)}</span>{"".join(pills)}</div>'
            f'<div style="font-size:24px;font-weight:650;letter-spacing:-.02em;line-height:30px;margin-top:6px">{K.esc(rep["title"])}</div>'
            f'<div class="sx-muted" style="font-size:13px;margin-top:2px">{K.esc(rep["subtitle"])}</div>')
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.download_button("Download PDF", lambda: X.to_pdf(rm.get(rid)), file_name=f"{stem}.pdf", mime="application/pdf",
                           icon=":material/picture_as_pdf:", type="primary", key="rv_pdf", on_click="ignore")
        st.download_button("Word", lambda: X.to_docx(rm.get(rid)), file_name=f"{stem}.docx", icon=":material/description:",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", key="rv_docx",
                           on_click="ignore")
        st.download_button("Markdown", report_markdown(snap), file_name=f"{stem}.md", mime="text/markdown",
                           icon=":material/markdown:", key="rv_md", on_click="ignore")
        email_buttons(rid)
        if log:
            last = log[0]
            who = last.get("list_name") or ", ".join(last.get("recipients") or [])
            st.html(f'<span class="sx-subtle" style="font-size:12px;margin-left:6px">{K.icon("mail", 13)} Emailed to '
                    f'{K.esc(who)} at {datetime.fromisoformat(last["sent_at"]):%b %-d %I:%M %p}</span>')
    st.html('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px 18px;margin-top:14px;'
            'padding-top:14px;border-top:1px solid #E5E2DE">' + "".join(
                f'<div><div class="sx-label">{K.esc(k)}</div><div style="font-size:13px;font-weight:500;margin-top:1px">{K.esc(v)}</div></div>'
                for k, v in rep["facts"]) + "</div>")

if ss.get("email_open") == rid:
    email_dialog(rid)


# -------------------------------------------------------------------- actions
def _track(run_id: str):
    for k, v in (("ca_q", run_id), ("ca_status", "all"), ("ca_view", "report"), ("ca_owner", "all"), ("ca_machine", "all")):
        ss[k] = v
        ss[f"w_{k}"] = v
    shell.goto("app_pages/corrective_actions.py")


if snap["status"] == "completed" and not capa_ops.actions(rid):
    capa_ops.ingest(snap)  # finished before tracking existed
acts = capa_ops.actions(rid)
if acts:
    now = time.time()
    stt = C.run_status(acts, now)
    pill = {"closed": K.pill("completed", stt["label"], "xs"), "reopened": K.pill("failed", stt["label"], "xs"),
            "verifying": K.pill("done", stt["label"], "xs")}.get(stt["key"], K.pill("running", stt["label"], "xs"))
    with K.card("rv_actions", accent=stt["overdue"] > 0):
        st.html(K.card_title("8D follow-up", f"{stt['closed']} of {stt['total']} actions closed"
                             + (f" · {stt['overdue']} overdue" if stt["overdue"] else ""), "list", pill))
        st.html(K.table([("discipline", "Step"), ("action", "Action"), ("owner_name", "Owner"), ("due", "Due"), ("status", "Status")], acts, {
            "discipline": lambda a: f'<span class="sx-mono" style="font-weight:600;color:#B54111">{K.esc(a["discipline"])}</span>',
            "action": lambda a: f'<span style="white-space:normal;color:#1B1816">{K.esc(a["action"])}</span>',
            "due": lambda a: (f'<b style="color:#B54111">Overdue</b> ' if C.is_overdue(a, now) else "") + K.esc(K.ts(a.get("due_at"))),
            "status": lambda a: K.esc(C.STATUS_LABEL[a["status"]])}))
        st.button("Track these actions", icon=":material/task:", key="rv_track", type="tertiary", on_click=_track, args=(rid,))

# ----------------------------------------------------------------------- body
jump = ([("summary", "Summary")] if rep["highlights"] else []) + [(s["id"], s["code"]) for s in rep["sections"]]
st.html('<nav class="sx-jump" aria-label="Report sections"><span class="sx-label">Jump to</span>'
        + "".join(f'<a href="#rv-{k}">{K.esc(v)}</a>' for k, v in jump)
        + (f'<span class="sx-subtle" style="font-size:11.5px;margin-left:6px">Not run: {K.esc(", ".join(rep["skipped"]))}</span>'
           if rep["skipped"] else "") + "</nav>")


def highlight(h: dict):
    with K.flat(f"hl_{h['key']}"):
        st.html(f'<div class="sx-row"><span class="sx-label" style="color:#B54111">{K.esc(h["label"])}</span>'
                f'<span class="sx-pill off xs sx-mono" style="margin-left:auto">{K.esc(h["step"])}</span></div>')
        with st.container(key=f"agentout_hl_{h['key']}"):
            st.markdown(K.md(X.clean_md(h["markdown"])))


main = st.container()
with main:
    if rep["highlights"]:
        with K.card("rv_summary"):
            st.html('<div id="rv-summary" class="sx-anchor"></div>' + K.card_title("Summary", "Key findings pulled from the agent sections", "file"))
            first, rest = rep["highlights"][0], rep["highlights"][1:4]
            highlight(first)
            text = [h for h in rest if "|" not in h["markdown"]]
            tables = [h for h in rest if "|" in h["markdown"]]
            for i in range(0, len(text), 2):
                cols = st.columns(2)
                for col, h in zip(cols, text[i:i + 2]):
                    with col:
                        highlight(h)
            for h in tables:
                highlight(h)

    for s in snap["steps"]:
        if s["status"] != "done":
            continue
        with K.card(f"rvsec_{s['id']}"):
            st.html(f'<div id="rv-{s["id"]}" class="sx-anchor"></div>'
                    f'<div class="sx-row" style="gap:12px;margin-bottom:10px"><span class="sx-code">{K.esc(s["code"])}</span>'
                    f'<div style="min-width:0"><div style="font-size:15px;font-weight:600;line-height:20px">{K.esc(s["title"])}</div>'
                    f'<div class="sx-subtle" style="font-size:12px">{K.esc(s["subtitle"])}</div></div>'
                    f'<span class="sx-subtle tabular" style="margin-left:auto;font-size:11.5px">'
                    f'{K.num((s.get("usage") or {}).get("total_tokens"))} tokens · {s.get("latency_s") or "—"}s</span></div>')
            ev = [tl for tl in s.get("tools", []) if tl.get("summary")]
            if ev:
                st.html('<div style="display:grid;gap:4px;margin-bottom:6px">' + "".join(
                    f'<div style="border-left:2px solid #F26B1D;padding:2px 10px;font-size:12px" class="sx-muted">'
                    f'<b style="color:#1B1816">Evidence · {K.esc(tl["label"])}:</b> {K.esc(tl["summary"])}</div>' for tl in ev) + "</div>")
            with st.container(key=f"agentout_rv_{s['id']}"):
                st.markdown(K.md(X.clean_md(s["output"])))
