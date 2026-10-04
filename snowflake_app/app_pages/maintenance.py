import time

import pandas as pd
import streamlit as st

from snowflake_app.services import impact_ops
from snowflake_app.ui import chat_dock, kit as K, maint, shell

PATH = "app_pages/maintenance.py"
shell.topbar(PATH)
mon = shell.monitor()
ss = st.session_state

LANES = [("planned", "Planned", "Caught early. Do the job before the forecast date."),
         ("awaiting", "Down · awaiting repair", "Stopped automatically or taken offline."),
         ("in_repair", "In repair", "Technician on the job."),
         ("commissioning", "Commissioning", "Running again while the model learns its new normal.")]


def kpis():
    k = mon.plant_kpis()
    avail = f"{k['availability']:.0f}" if k["availability"] is not None else "—"
    cells = [
        K.kpi("Plant availability", avail, "%" if k["availability"] is not None else "",
              f"{k['total'] - len(k['down'])} of {k['total']} machines running" if k["total"] else "Start monitoring to see live state",
              ico="gauge", emphasis=bool(k["down"])),
        K.kpi("Machines down", str(len(k["down"])), "", ", ".join(k["down"]) or "None", ico="wrench"),
        K.kpi("Downtime, 7 days", f"{k['downtime_hours_7d']:.1f}", "h", "Unplanned and planned stops", ico="clock"),
        K.kpi("Caught early", str(k["planned"]), "", f"Saves {K.usd(k['avoided_cost_usd'])}, {k['avoided_hours']:g} h vs. breakdowns",
              ico="shield"),
        K.kpi("Early warning", K.duration(k["lead_hours_median"]) if k["lead_hours_median"] else "—", "",
              "Median notice before failure", ico="trend"),
    ]
    st.html(f'<div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px">{"".join(cells)}</div>')
    return k


def lane(status: str, title: str, hint: str, wos: list[dict], live: dict, impact: dict):
    items = [w for w in wos if w["status"] == status]
    with K.card(f"lane_{status}"):
        st.html(K.card_title(title, hint, None, f'<span class="sx-pill off xs tabular">{len(items)}</span>'))
        if not items:
            st.html('<div class="sx-subtle" style="font-size:12px;padding:6px 0">Nothing here.</div>')
        for w in sorted(items, key=lambda x: (x["priority"], x.get("created_at") or "")):
            st.html(maint.wo_card_html(w, live=live.get(w["machine_id"]), impact=impact.get(w["machine_id"])))
            if status == "commissioning":
                m = live.get(w["machine_id"]) or {}
                if m.get("learning_progress") is not None:
                    st.html(K.bar(m["learning_progress"], True, True, "Commissioning progress"))
            maint.wo_actions(w, f"wo_{w['wo_id']}")


def stored_forecasts() -> dict:
    """Monitoring off: the last persisted time to failure, counted down since it was saved."""
    out = {}
    for r in mon.store.all("machine_state"):
        h, at = r.get("rul_hours"), maint._epoch(r.get("updated_at"))
        if h is not None and at and r.get("lifecycle") == "running":
            out[r["machine_id"]] = {"rul": {"hours": max(h - (time.time() - at) / 3600, 0)}}
    return out


@st.fragment(run_every=shell.ticking(2))
def board():
    shell.follow_nav()
    maint.fragment_hook()
    chat_dock.fragment_hook()
    k = kpis()
    wos = mon.work_orders()
    snap = mon.snapshot() if mon.engine.session_id else {"machines": []}
    live = {m["machine_id"]: m for m in snap["machines"]} or stored_forecasts()
    try:
        impact = impact_ops.plant(mon.outlook(), wos, shell.settings())["by_id"]
    except Exception:
        impact = {}  # cards still render without the business line
    if not mon.active:
        st.html('<div class="sx-inset" style="padding:10px 12px;font-size:12.5px;margin-top:10px">'
                f'{K.icon("info", 14)} Live monitoring is off. Work orders can still be progressed; a repaired machine '
                'learns its new normal when monitoring starts again.</div>')
    for row in (LANES[:2], LANES[2:]):
        cols = st.columns(2)
        for col, (status, title, hint) in zip(cols, row):
            with col:
                lane(status, title, hint, wos, live, impact)

    done = [w for w in wos if w["status"] in ("closed", "cancelled")]
    with K.card("closed"):
        st.html(K.card_title("History", f"{len(done)} closed or cancelled work orders", "list"))
        if not done:
            st.caption("Closed work orders appear here with their downtime and cost.")
            return
        df = pd.DataFrame([{
            "Work order": w["wo_id"], "Machine": f"{w['machine_id']} · {w.get('machine_name') or ''}",
            "Type": "Planned" if w["kind"] == "planned" else "Breakdown", "Component": w["component"],
            "Status": w["status"].capitalize(), "Opened": K.ts(w.get("created_at")), "Closed": K.ts(w.get("closed_at")),
            "Downtime (h)": w.get("downtime_hours"), "Est. cost": w.get("est_cost_usd"),
            "Technician": w.get("technician_name") or "", "Notes": w.get("notes") or "",
            "Parts used": ", ".join(f"{p.get('qty', 1)}× {p.get('part_name')}" for p in w.get("parts_used") or []),
        } for w in done])
        st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 360),
                     column_config={"Est. cost": st.column_config.NumberColumn(format="$%d"),
                                    "Downtime (h)": st.column_config.NumberColumn(format="%.1f")})


maint.page_dialog()
board()
