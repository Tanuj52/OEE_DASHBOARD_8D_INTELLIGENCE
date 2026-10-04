"""
Maintenance UI shared by Live Monitoring and the Maintenance page: work-order
cards, lifecycle action callbacks and the complete-repair dialog.

Buttons live inside auto-refreshing fragments, so they act through on_click
callbacks. The dialog is opened at page level: a callback records the request,
the fragment asks for one full rerun, and the page script renders the dialog.
"""

import time
from datetime import datetime

import streamlit as st

from snowflake_app.core import maintenance as MX
from snowflake_app.services import parts_ops
from snowflake_app.ui import kit as K, shell

ss = st.session_state


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


# ------------------------------------------------------------------ callbacks
def schedule(mid: str):
    wo = shell.monitor().schedule_maintenance(mid)
    shell.notify(f"Work order {wo['wo_id']} planned for {mid}", icon=":material/build:")


def take_offline(mid: str):
    shell.monitor().take_offline(mid)
    shell.notify(f"{mid} taken offline for maintenance", icon=":material/power_settings_new:")


def start_repair(wo_id: str):
    wo = shell.monitor().store.get("work_orders", wo_id) or {}
    tech = {"id": wo.get("technician_id"), "name": wo.get("technician_name")} if wo.get("technician_id") else None
    shell.monitor().start_repair(wo_id, tech)
    shell.notify(f"Repair started on {wo.get('machine_id', '')}", icon=":material/construction:")


def cancel(wo_id: str):
    shell.monitor().cancel_work_order(wo_id)
    shell.notify(f"Work order {wo_id} cancelled", icon=":material/close:")


def request_complete(wo_id: str):
    ss.complete_wo = wo_id


def order_parts(wo_id: str):
    """Purchase requests for the parts this job is short of (vendor minimum quantity)."""
    wo = shell.monitor().store.get("work_orders", wo_id) or {}
    info = parts_ops.by_id()
    made = []
    for p in parts_ops.wo_parts(wo):
        if not p["covered"] and not p["incoming"]:
            m = info.get(p["part_id"]) or {}
            po = parts_ops.create_po(p["part_id"], max(int(m.get("min_order_qty") or 1), 1),
                                     reason=f"Short for {wo_id} ({wo.get('machine_id')})", wo_id=wo_id, user=shell.current_user())
            made.append(po["po_id"])
    shell.notify(f"Purchase request {', '.join(made)} raised" if made else "Nothing to order: the parts are covered or on order",
                 icon=":material/shopping_cart:")


def view_machine(mid: str):
    ss.focus_machine = mid
    ss.follow_risk = False
    ss.w_follow_risk = False
    shell.goto("app_pages/live_monitoring.py")


# ------------------------------------------------------------------ dialog
def fragment_hook():
    """Call inside a refreshing fragment: escalate a pending dialog request to one full rerun."""
    if ss.get("complete_wo") and ss.get("_dlg_open") != ss.complete_wo:
        st.rerun(scope="app")


def _close():
    for k in [k for k in ss if str(k).startswith("cr_q_")]:
        ss.pop(k, None)
    ss.pop("complete_wo", None)
    ss.pop("_dlg_open", None)


def _submit(wo_id: str):
    wo = shell.monitor().store.get("work_orders", wo_id) or {}
    used = [{"part_id": p["part_id"], "qty": int(ss.get(f"cr_q_{p['part_id']}") or 0)} for p in _usable(wo)]
    shell.monitor().complete_repair(wo_id, notes=ss.get("cr_notes", ""), parts_used=[u for u in used if u["qty"] > 0],
                                    user=shell.current_user())
    shell.notify(f"Repair completed. {wo.get('machine_id', '')} is commissioning.", icon=":material/task_alt:")
    _close()


def _usable(wo: dict) -> list[dict]:
    """Parts a technician may have used: the reserved ones first, then everything compatible with the machine."""
    out = {p["part_id"]: p for p in wo.get("parts") or []}
    try:
        for p in MX.parts_for(wo["machine_id"], None):
            out.setdefault(p["part_id"], p)
    except Exception:
        pass
    return list(out.values())


@st.dialog("Complete repair", width="medium", icon=":material/task_alt:", on_dismiss=_close)
def _dialog(wo_id: str):
    wo = shell.monitor().store.get("work_orders", wo_id)
    if not wo or wo["status"] != "in_repair":
        st.info("This work order is no longer in repair.", icon=":material/info:")
        return
    started = _epoch(wo.get("started_at"))
    st.html(f'<div class="sx-row" style="gap:8px;flex-wrap:wrap"><span class="sx-mono" style="font-weight:600">{K.esc(wo_id)}</span>'
            f'{K.pill(wo["priority"], size="xs")}</div>'
            f'<div style="font-size:15px;font-weight:600;margin-top:4px">{K.esc(wo["machine_id"])} · {K.esc(wo.get("machine_name") or "")}</div>'
            f'<div class="sx-muted" style="font-size:12.5px">{K.esc(wo["component"])} · in repair for '
            f'{K.seconds(time.time() - started) if started else "—"} (estimate {wo["est_hours"]:g} h)</div>')
    with st.form("complete_repair", border=False):
        st.text_area("Work done", key="cr_notes", height=90, placeholder="What was found and fixed")
        usable = _usable(wo)
        if usable:
            held = parts_ops.held(wo_id)
            st.html('<div class="sx-label">Parts used</div><div class="sx-subtle" style="font-size:12px">Taken from the store. '
                    'Reserved parts not used go back on the shelf.</div>')
            cols = st.columns(2)
            for i, p in enumerate(usable):
                key = f"cr_q_{p['part_id']}"
                ss.setdefault(key, held.get(p["part_id"], 0))
                cols[i % 2].number_input(p["part_name"] + (" (reserved)" if p["part_id"] in held else ""), 0, 50, step=1, key=key)
        st.caption("Completing the repair puts the machine into commissioning: it streams again while the model learns its new "
                   "normal, with alerts off. It returns to service with a new model version.")
        st.form_submit_button("Complete repair and commission", type="primary", icon=":material/task_alt:",
                              on_click=_submit, args=(wo_id,))


def page_dialog():
    """Call once at page level (outside fragments)."""
    wo_id = ss.get("complete_wo")
    if wo_id:
        ss._dlg_open = wo_id
        _dialog(wo_id)


# ------------------------------------------------------------------ pieces
def wo_status_line(wo: dict, live: dict | None = None) -> str:
    now = time.time()
    if wo["status"] == "planned":
        rul = wo.get("rul_hours_at_open")
        created = _epoch(wo.get("created_at")) or now
        due = (rul - (now - created) / 3600) if rul is not None else None
        live_h = ((live or {}).get("rul") or {}).get("hours")
        if live_h is not None:
            due = live_h  # the current forecast beats the one taken when the order was raised
        return f"Do before {K.by_when(due)}" if due is not None else "Planned"
    if wo["status"] in ("awaiting", "in_repair"):
        down = _epoch(wo.get("down_at"))
        word = "Down" if wo["status"] == "awaiting" else "In repair"
        return f"{word} {K.seconds(now - down)}" if down else word
    if wo["status"] == "commissioning":
        return "Learning the new normal"
    if wo["status"] == "closed":
        return f"Back in service · {wo['downtime_hours']:.1f} h down" if wo.get("downtime_hours") is not None else "Closed"
    return wo["status"].capitalize()


def parts_line(wo: dict) -> str:
    """Live part status from the stock ledger: held for this job, or short with the lead time."""
    if not wo.get("parts"):
        return '<span class="sx-subtle">No matching spares listed for this machine</span>'
    try:
        rows = parts_ops.wo_parts(wo)
    except Exception:
        rows = []
    out = []
    closed = wo["status"] in ("commissioning", "closed", "cancelled")
    for p in rows[:3]:
        if closed:
            note, col = "", "#57524D"
        elif p["covered"]:
            note, col = " (held)" if p["reserved"] else f" ({p['available']} free)", "#57524D"
        else:
            eta = f", {p['incoming']} on order" if p["incoming"] else f", order now: {p['lead_time_days']} d lead time"
            note, col = f" (short{eta})", "#B54111"
        out.append(f'<span style="color:{col}">{K.esc(p["part_name"])}{K.esc(note)}</span>')
    return " · ".join(out) or '<span class="sx-subtle">No matching spares listed for this machine</span>'


def impact_line(imp: dict | None) -> str:
    """One line of business impact for a work order (services/impact_ops.machine)."""
    if not imp:
        return ""
    sc, win = imp.get("scenario"), imp.get("window")
    if win:
        hit = ("no customer orders affected" if not win["late_count"] and not win["tight_count"]
               else f"{K.count(win['late_count'], 'order')} late" if win["late_count"] else f"{K.count(win['tight_count'], 'order')} tight")
        save = f" · saves {K.usd(win['saves'])} vs. a breakdown" if win["saves"] > 0 else ""
        txt = f"Best time: start {K.by_when(win['start_in_h'])}, {hit}{save}"
        hot = bool(win["late_count"])
    elif sc and sc["kind"] == "down":
        per_h = sc["cost_per_h"]["total_h"]
        late = (f"{K.count(sc['late_count'], 'order')} late ({K.usd(sc['value_at_risk'])})" if sc["late_count"]
                else f"{K.count(sc['tight_count'], 'order')} tight" if sc["tight_count"] else "no orders late if fixed in time")
        txt = f"Costs {K.usd(per_h)}/h down · {late}"
        hot = bool(sc["late_count"])
    elif sc:
        txt = (f"If it fails: {K.count(sc['late_count'], 'order')} late ({K.usd(sc['value_at_risk'])})" if sc["late_count"]
               else f"If it fails: {K.usd(sc['total_cost'])}, no orders late")
        hot = bool(sc["late_count"])
    else:
        return ""
    return (f'<div style="font-size:12px;margin-top:4px;color:{"#B54111" if hot else "#57524D"}">'
            f'{K.icon("dollar", 12)} {K.esc(txt)}</div>')


def wo_card_html(wo: dict, compact: bool = False, live: dict | None = None, impact: dict | None = None) -> str:
    status = {"awaiting": "awaiting", "in_repair": "in_repair", "commissioning": "commissioning", "planned": "planned",
              "closed": "closed", "cancelled": "cancelled"}.get(wo["status"], "pending")
    hot = wo["priority"] == "P1" and wo["status"] in ("awaiting", "in_repair")
    border = "#F26B1D" if hot else "#E5E2DE"
    tech = f'{K.icon("user", 12)} {K.esc(wo["technician_name"])}' if wo.get("technician_name") else ""
    extra = "" if compact else (
        f'<div class="sx-muted" style="font-size:12px;margin-top:6px">{K.icon("box", 12)} {parts_line(wo)}</div>'
        + (impact_line(impact) if wo["status"] in ("planned", "awaiting", "in_repair") else "") +
        f'<div class="sx-subtle" style="font-size:11.5px;margin-top:3px">{tech}'
        f'{" · " if tech else ""}est. {wo["est_hours"]:g} h · {K.usd(wo["est_cost_usd"])}</div>')
    return (f'<div style="border:1px solid {border};border-radius:12px;background:#fff;padding:10px 12px;min-width:0">'
            f'<div class="sx-row" style="justify-content:space-between;gap:6px"><span class="sx-mono" style="font-size:11.5px;color:#57524D">{K.esc(wo["wo_id"])}</span>'
            f'{K.pill(wo["priority"], size="xs")}</div>'
            f'<div style="font-size:13.5px;font-weight:600;margin-top:4px">{K.esc(wo["machine_id"])} · {K.esc(wo.get("machine_name") or "")}</div>'
            f'<div class="sx-muted" style="font-size:12px">{K.esc(wo["component"])}</div>'
            f'<div class="sx-row" style="gap:6px;margin-top:6px">{K.pill(status, size="xs")}'
            f'<span class="tabular" style="font-size:12px;font-weight:500">{K.esc(wo_status_line(wo, live))}</span></div>{extra}</div>')


def wo_actions(wo: dict, key: str, show_view: bool = True):
    """Lifecycle buttons for one work order."""
    s = wo["status"]
    with st.container(horizontal=True, gap="small"):
        if s == "planned":
            st.button("Start repair", key=f"{key}_start", icon=":material/construction:", type="primary",
                      on_click=start_repair, args=(wo["wo_id"],), help="Takes the machine offline and starts the job")
            st.button("Take offline", key=f"{key}_off", icon=":material/power_settings_new:",
                      on_click=take_offline, args=(wo["machine_id"],))
            st.button("Cancel", key=f"{key}_cancel", type="tertiary", on_click=cancel, args=(wo["wo_id"],))
        elif s == "awaiting":
            st.button("Start repair", key=f"{key}_start", icon=":material/construction:", type="primary",
                      on_click=start_repair, args=(wo["wo_id"],))
        elif s == "in_repair":
            st.button("Complete repair", key=f"{key}_done", icon=":material/task_alt:", type="primary",
                      on_click=request_complete, args=(wo["wo_id"],))
        if show_view and s != "closed":
            st.button("View machine", key=f"{key}_view", type="tertiary", icon=":material/monitor_heart:",
                      on_click=view_machine, args=(wo["machine_id"],))
        if s in ("planned", "awaiting", "in_repair") and wo.get("parts"):
            try:
                short = [p for p in parts_ops.wo_parts(wo) if not p["covered"] and not p["incoming"]]
            except Exception:
                short = []
            if short:
                st.button("Order parts", key=f"{key}_buy", type="tertiary", icon=":material/shopping_cart:",
                          on_click=order_parts, args=(wo["wo_id"],),
                          help="Raise purchase requests for: " + ", ".join(p["part_name"] for p in short))


def checklist_html(wo: dict) -> str:
    items = "".join(f'<li style="display:grid;grid-template-columns:20px 1fr;gap:8px;list-style:none;font-size:12.5px">'
                    f'<span style="width:20px;height:20px;border-radius:6px;background:#FFF1E8;color:#B54111;font-size:11px;font-weight:700;'
                    f'display:flex;align-items:center;justify-content:center">{i}</span><span>{K.esc(t)}</span></li>'
                    for i, t in enumerate(wo.get("checklist") or MX.CHECKLIST.get(wo.get("failure_mode") or "", []), 1))
    return f'<ol style="margin:6px 0 0;padding:0;display:grid;gap:6px">{items}</ol>'
