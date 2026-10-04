"""
Daily digest email: what needs attention today, in one message.

  corrective actions  overdue, and due in the next three days, with owners
  spare parts         stock-outs and parts at or below the reorder point
  purchasing          requests waiting for approval, deliveries due soon
  customer orders     orders a stopped machine (or one forecast to fail within
                      the planning horizon) will make late, with their value

build() renders it from plain rows. The app calls it with its own state for
"Send now" and the preview; the SEND_DAILY_DIGEST stored procedure calls it
with the same rows read from the OPERATIONS tables every morning
(sql/procs.py). risk_from_state() is the procedure's view of order risk, from
stored machine state and open work orders. Standard library only.
"""

from datetime import datetime
from html import escape

from snowflake_app.core import impact as I

ORANGE, INK, SUBTLE, LINE, SOFT = "#F26B1D", "#1B1816", "#78716B", "#E5E2DE", "#F5F4F2"
OPEN_WO = ("planned", "awaiting", "in_repair", "commissioning")
UNPLANNED_H = 5.7  # plant median repair time for a breakdown when a work order has no estimate


def _ts(v) -> float | None:
    try:
        return datetime.fromisoformat(str(v)).timestamp() if v else None
    except ValueError:
        return None


def risk_from_state(states: list[dict], work_orders: list[dict], book: list[dict], rates: dict, customers: dict,
                    now: float, horizon_h: float = 72.0, cfg: dict | None = None) -> list[dict]:
    """Orders at risk per machine from stored state: the current stop of a down machine, or a failure
    at the forecast time for a running one within the horizon."""
    wo = {}
    for w in work_orders:
        if w.get("status") in OPEN_WO:
            wo.setdefault(w["machine_id"], w)
    out = []
    for s in states:
        mid = s["machine_id"]
        orders = I.open_orders(book, mid)
        w = wo.get(mid) or {}
        rate = rates.get(mid, 1.0)
        if s.get("lifecycle") in ("tripped", "stopped", "in_repair"):
            start = _ts(s.get("down_since")) or _ts(w.get("down_at")) or now
            est = float(w.get("est_hours") or UNPLANNED_H)
            if w.get("status") == "in_repair" and _ts(w.get("started_at")):
                est = max(est - (now - _ts(w["started_at"])) / 3600, 0.25)
            sc = I.stop_cost(orders, rate, start, (now - start) / 3600 + est, float(w.get("est_cost_usd") or 0), cfg, customers)
            kind = "down"
        else:
            h, at = s.get("rul_hours"), _ts(s.get("updated_at"))
            if h is None or not at:
                continue
            h = max(float(h) - (now - at) / 3600, 0)
            if h > horizon_h:
                continue
            sc = I.stop_cost(orders, rate, now + h * 3600, UNPLANNED_H, 0, cfg, customers)
            kind = "forecast"
        if sc["late"] or sc["tight_count"]:
            out.append({"machine_id": mid, "name": s.get("machine_name") or mid, "kind": kind, "late": sc["late"],
                        "tight": sc["tight_count"], "value_at_risk": sc["value_at_risk"], "penalty_usd": sc["penalty_usd"],
                        "customers": sc["customers"], "total_cost": sc["total_cost"]})
    return out


def _table(head: list[str], rows: list[list[str]], right: set = frozenset()) -> str:
    th = "".join(f'<th align="{"right" if i in right else "left"}" style="background:{SOFT};font-size:11px;color:{SUBTLE};'
                 f'padding:6px 8px;border-bottom:1px solid {LINE}">{escape(h)}</th>' for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f'<td align="{"right" if i in right else "left"}" style="font-size:12.5px;color:{INK};'
                                   f'padding:6px 8px;border-bottom:1px solid {LINE};vertical-align:top">{escape(str(c))}</td>'
                                   for i, c in enumerate(r)) + "</tr>" for r in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            f'border:1px solid {LINE};margin:0 0 6px">{th and "<tr>" + th + "</tr>"}{trs}</table>')


def _section(title: str, sub: str, body: str) -> str:
    return (f'<tr><td style="padding:18px 0 4px"><p style="margin:0;font-size:15px;font-weight:700;color:{INK}">{escape(title)}</p>'
            f'<p style="margin:2px 0 8px;font-size:12px;color:{SUBTLE}">{escape(sub)}</p>{body}</td></tr>')


def _when(iso) -> str:
    t = _ts(iso)
    return datetime.fromtimestamp(t).strftime("%b %-d %H:%M") if t else "—"


def build(actions: list[dict], stock: list[dict], pos: list[dict], risk: list[dict], now: float | None = None,
          plant: str = "8D Intelligence") -> dict:
    """{subject, html, counts}. stock rows: part_id, part_name, on_hand, reserved, available, reorder_point,
    vendor_name, lead_time_days[, incoming]. risk rows: see risk_from_state()."""
    now = now or datetime.now().timestamp()
    opened = [a for a in actions if a.get("status") in ("open", "in_progress")]
    overdue = sorted((a for a in opened if (_ts(a.get("due_at")) or now + 1) < now), key=lambda a: str(a.get("due_at")))
    soon = sorted((a for a in opened if now <= (_ts(a.get("due_at")) or 0) < now + 3 * 86400), key=lambda a: str(a.get("due_at")))
    to_order = [s for s in stock if int(s.get("available") or 0) + int(s.get("incoming") or 0) <= int(s.get("reorder_point") or 0)
                or int(s.get("available") or 0) < 0]
    approve = [p for p in pos if p.get("status") == "requested"]
    arriving = [p for p in pos if p.get("status") == "ordered" and (_ts(p.get("expected_at")) or now) < now + 3 * 86400]
    late = [o for r in risk for o in r["late"]]
    value = sum(float(o["order_value_usd"]) for o in late)
    counts = {"overdue_actions": len(overdue), "due_soon": len(soon), "parts_to_order": len(to_order),
              "po_to_approve": len(approve), "orders_at_risk": len(late), "value_at_risk": round(value, 2)}
    tiles = [("Overdue actions", len(overdue)), ("Parts to order", len(to_order)), ("Requests to approve", len(approve)),
             ("Orders at risk", len(late))]
    tile_html = "".join(f'<td width="25%" style="padding:0 4px"><div style="border:1px solid {LINE};border-radius:8px;padding:10px 12px">'
                        f'<div style="font-size:10.5px;font-weight:700;letter-spacing:.06em;color:{SUBTLE}">{escape(k.upper())}</div>'
                        f'<div style="font-size:22px;font-weight:700;color:{ORANGE if v else INK};margin-top:2px">{v}</div></div></td>'
                        for k, v in tiles)
    body = []
    if overdue or soon:
        rows = [[("Overdue · " if a in overdue else "") + _when(a.get("due_at")), a.get("owner_name") or "—",
                 f"{a.get('discipline')} {a.get('action')}", f"{a.get('machine_id') or ''} · {a.get('run_id')}"] for a in (overdue + soon)[:14]]
        body.append(_section("Corrective actions", f"{len(overdue)} overdue, {len(soon)} due in the next 3 days",
                             _table(["Due", "Owner", "Action", "Machine · 8D"], rows)))
    if to_order:
        rows = [[s["part_name"], s.get("on_hand"), s.get("reserved"), s.get("available"), s.get("reorder_point"),
                 f"{s.get('vendor_name') or '—'} · {s.get('lead_time_days') or '—'} d"] for s in to_order[:12]]
        body.append(_section("Spare parts to order", "At or below the reorder point after reservations and open requests",
                             _table(["Part", "On hand", "Held", "Free", "Reorder at", "Vendor · lead time"], rows, {1, 2, 3, 4})))
    if approve or arriving:
        rows = [[p["po_id"], p.get("status", "").capitalize(), f"{p.get('qty')} × {p.get('part_name')}", p.get("vendor_name") or "—",
                 _when(p.get("expected_at")) if p.get("status") == "ordered" else "—"] for p in (approve + arriving)[:12]]
        body.append(_section("Purchasing", f"{len(approve)} requests waiting for approval, {len(arriving)} deliveries due in 3 days",
                             _table(["Order", "Status", "What", "Vendor", "Expected"], rows)))
    if late:
        rows = [[f"{r['machine_id']} {r['name'] if r['name'] != r['machine_id'] else ''}".strip(),
                 "Down now" if r["kind"] == "down" else "Forecast to fail", o["order_id"], o.get("customer_name") or "—",
                 str(o["due_date"])[:10], f"${float(o['order_value_usd']):,.0f}"] for r in risk for o in r["late"]][:14]
        body.append(_section("Customer orders at risk", f"{len(late)} order{'s' if len(late) != 1 else ''} worth ${value:,.0f} would ship late",
                             _table(["Machine", "Why", "Order", "Customer", "Due", "Value"], rows, {5})))
    if not body:
        body.append(_section("All clear", "No overdue actions, no parts to order and no orders at risk today.", ""))
    day = datetime.fromtimestamp(now).strftime("%A, %b %-d")
    subject = (f"8D daily digest · {day}: {len(overdue)} overdue actions, {len(to_order)} parts to order, "
               f"{len(late)} orders at risk")
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#F3F2F0">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F2F0;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#FFFFFF;border-radius:12px;
 overflow:hidden;font-family:Segoe UI,Helvetica,Arial,sans-serif">
<tr><td style="background:{ORANGE};padding:18px 26px"><p style="margin:0;font-size:11px;font-weight:700;letter-spacing:.08em;color:#FFFFFF">DAILY DIGEST</p>
<p style="margin:6px 0 0;font-size:20px;font-weight:700;color:#FFFFFF">{escape(day)}</p></td></tr>
<tr><td style="padding:18px 22px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{tile_html}</tr></table></td></tr>
<tr><td style="padding:0 26px 18px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(body)}</table></td></tr>
<tr><td style="padding:14px 26px;background:{SOFT};font-size:11.5px;color:{SUBTLE}">Sent by {escape(plant)} every morning to lists marked
 "Daily digest". Open the app to update actions, approve purchase requests or move work to another machine.</td></tr>
</table></td></tr></table></body></html>"""
    return {"subject": subject, "html": html, "counts": counts}
