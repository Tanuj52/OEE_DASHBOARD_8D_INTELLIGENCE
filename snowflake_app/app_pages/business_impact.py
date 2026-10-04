import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from snowflake_app.core import data as D, impact as I
from snowflake_app.services import impact_ops
from snowflake_app.ui import chat_dock, kit as K, maint, shell

PATH = "app_pages/business_impact.py"
shell.topbar(PATH)
ss = st.session_state
mon = shell.monitor()
ss.setdefault("bi_machine", "all")


def late_table(rows: list[dict]) -> str:
    return K.table([("order_id", "Order"), ("customer_name", "Customer"), ("due_date", "Due"), ("order_value_usd", "Value"),
                    ("status", "Outlook"), ("late_h", "Late by"), ("penalty_usd", "Penalty")], rows, {
        "order_id": lambda o: f'<span class="sx-mono">{K.esc(o["order_id"])}</span> <span class="sx-subtle">{K.esc(o["priority"])}</span>',
        "due_date": lambda o: K.esc(str(o["due_date"])[:10]),
        "order_value_usd": lambda o: K.usd(o["order_value_usd"]),
        "status": lambda o: {"late": K.pill("critical", "Late", "xs"), "tight": K.pill("warning", "Tight", "xs")}.get(
            o["status"], K.pill("ok", "Delayed, on time", "xs")),
        "late_h": lambda o: K.esc(K.duration(o["late_h"])) if o["late_h"] > 0 else f'<span class="sx-subtle">{K.duration(o["slack_h"])} spare</span>',
        "penalty_usd": lambda o: K.usd(o["penalty_usd"]) if o["penalty_usd"] else "—"})


def machine_card(r: dict, key: str):
    sc, win = r["scenario"], r.get("window")
    down = sc["kind"] == "down"
    title = f'{r["machine_id"]} · {r["name"]}'
    if down:
        since = datetime.fromtimestamp(sc["since"]).astimezone().isoformat(timespec="seconds")
        sub = (f'Down since {K.ts(since)}, expected back in about {K.duration(sc["remaining_h"])} · '
               f'{K.usd(sc["cost_per_h"]["total_h"])} per hour down')
    else:
        sub = f'Forecast to fail in about {K.duration(sc["in_h"])} (around {K.by_when(sc["in_h"])}) · {K.usd(sc["cost_per_h"]["total_h"])} per hour down'
    badge = (K.pill("critical", K.count(sc["late_count"], "order") + " late", "xs") if sc["late_count"]
             else K.pill("warning", f'{sc["tight_count"]} tight', "xs") if sc["tight_count"] else K.pill("healthy", "No orders late", "xs"))
    with K.card(f"bi_{key}_{r['machine_id']}", accent=bool(sc["late_count"])):
        st.html(K.card_title(title, sub, "wrench" if down else "trend", badge))
        facts = [("Value at risk", K.usd(sc["value_at_risk"])), ("Late penalties", K.usd(sc["penalty_usd"])),
                 ("Customers", ", ".join(sc["customers"]) or "—"),
                 ("Total cost of this stop" if down else "Cost if it fails", K.usd(sc["total_cost"]))]
        st.html('<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px 14px;margin-bottom:8px">' + "".join(
            f'<div><div class="sx-label">{K.esc(k)}</div><div class="tabular" style="font-size:13.5px;font-weight:600">{K.esc(v)}</div></div>'
            for k, v in facts) + "</div>")
        if sc["orders"]:
            st.html(late_table(sorted(sc["orders"], key=lambda o: (o["status"] != "late", -o["late_h"]))[:8]))
        else:
            st.html('<div class="sx-subtle" style="font-size:12.5px">No open orders are delayed by this stop.</div>')
        if win:
            hit = ("no orders affected" if not win["late_count"] and not win["tight_count"] else f'{win["late_count"]} late, {win["tight_count"]} tight')
            st.html(f'<div class="sx-inset" style="padding:9px 11px;font-size:12.5px;margin-top:8px">{K.icon("clock", 13)} '
                    f'<b>Best window for the planned job:</b> start {K.esc(K.by_when(win["start_in_h"]))} ({win["hours"]:g} h), {K.esc(hit)}. '
                    f'Costs {K.usd(win["cost"])} instead of {K.usd(sc["total_cost"])} for a breakdown'
                    + (f', saving <b>{K.usd(win["saves"])}</b>' if win["saves"] > 0 else "") + ".</div>")
        alt_ok = [a for a in r.get("alternates") or [] if a["covers"]]
        if sc["late_count"] and r.get("alternates"):
            a0 = (alt_ok or r["alternates"])[0]
            need = I.work_hours(sc["late"], r["rate_h"])
            st.html(f'<div class="sx-inset" style="padding:9px 11px;font-size:12.5px;margin-top:8px">{K.icon("arrow", 13)} '
                    f'<b>Alternate machine:</b> {K.esc(a0["machine_id"])} {K.esc(a0.get("name") or "")} has {a0["spare_hours_7d"]:g} h free '
                    f'this week; the late orders need about {need:g} h'
                    + ("." if a0["covers"] else ", so it can take part of them.") + "</div>")
        with st.container(horizontal=True, gap="small"):
            wo = r.get("work_order")
            if wo:
                maint.wo_actions(wo, f"bi_{key}", show_view=True)
            elif not down:
                st.button("Schedule maintenance", key=f"bi_sched_{r['machine_id']}", icon=":material/build:", type="primary",
                          on_click=maint.schedule, args=(r["machine_id"],))


@st.fragment(run_every=shell.ticking(4))
def risk_section():
    shell.follow_nav()
    maint.fragment_hook()
    chat_dock.fragment_hook()
    p = impact_ops.plant(mon.outlook(), mon.work_orders(), shell.settings())
    k = p["kpis"]
    ss._bi_plant = p
    tiles = [
        K.kpi("Plant OEE", f"{k['oee'] * 100:.1f}" if k["oee"] is not None else "—", "%" if k["oee"] is not None else "",
              "Availability × performance × quality", ico="gauge"),
        K.kpi("Downtime cost, 7 days", K.usd(k["downtime_cost_7d"]), "", "Lost margin and idle crew", ico="dollar",
              emphasis=k["downtime_cost_7d"] > 0),
        K.kpi("Orders late now", str(k["late_now"]), "", f"{K.usd(k['value_now'])} · {K.usd(k['penalty_now'])} penalties",
              ico="truck", emphasis=k["late_now"] > 0),
        K.kpi("At risk if forecasts hold", str(k["late_forecast"]), "", f"{K.usd(k['value_forecast'])} of orders", ico="trend"),
        K.kpi("Customers affected", str(len(k["customers"])), "", ", ".join(k["customers"]) or "None", ico="users"),
    ]
    st.html(f'<section class="sx-kpis" aria-label="Business indicators">{"".join(tiles)}</section>')
    if not mon.active:
        st.html('<div class="sx-inset" style="padding:10px 12px;font-size:12.5px;margin-top:10px">'
                f'{K.icon("info", 14)} Live monitoring is off: risk uses the last stored machine state and forecasts.</div>')
    if not p["down"] and not p["forecast"]:
        with K.card("bi_clear"):
            st.html(K.card_title("Orders at risk", None, "shield") +
                    '<div class="sx-subtle" style="font-size:12.5px">No machine is down or forecast to fail within the planning horizon, '
                    'so every open order is on schedule.</div>')
    for r in sorted(p["down"], key=lambda r: -r["scenario"]["value_at_risk"]):
        machine_card(r, "dn")
    for r in sorted(p["forecast"], key=lambda r: (r["scenario"]["in_h"])):
        machine_card(r, "fc")


maint.page_dialog()
risk_section()
p = ss.get("_bi_plant") or impact_ops.plant(mon.outlook(), mon.work_orders(), shell.settings())

# ------------------------------------------------------------------ order book
with K.card("bi_book"):
    st.html(K.card_title("Order book", f"{p['kpis']['open_orders']} open customer orders worth {K.usd(p['kpis']['open_value'])}, "
                         "rolled forward every night in Snowflake", "list"))
    now = time.time()
    summary = []
    for r in p["machines"]:
        hrs = I.work_hours(r["orders"], r["rate_h"])
        summary.append({"Machine": f"{r['machine_id']} · {r['name']}", "Line": r["line"], "Open orders": r["open_orders"],
                        "Value": r["order_value"], "Work (h)": hrs, "Next 7 days load": round(100 * (7 * 24 - I.spare_hours(r["orders"], r["rate_h"], now)) / (7 * 24)),
                        "Cost per hour down": r["cost_per_h"]["total_h"], "Rate (units/h)": r["rate_h"],
                        "Next due": min((str(o["due_date"])[:10] for o in r["orders"]), default="—")})
    st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch", height=min(38 + 35 * len(summary), 420),
                 column_config={"Value": st.column_config.NumberColumn(format="$%.0f"),
                                "Cost per hour down": st.column_config.NumberColumn(format="$%.0f"),
                                "Next 7 days load": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=100)})
    ids = ["all"] + [r["machine_id"] for r in p["machines"]]
    shell.synced(st.selectbox, "Orders for", ids, state="bi_machine", width=220,
                 format_func=lambda i: "All machines" if i == "all" else f"{i} · {p['by_id'][i]['name']}")
    orders = [o for r in p["machines"] for o in r["orders"] if ss.bi_machine in ("all", r["machine_id"])]
    names = D.customer_names()
    st.dataframe(pd.DataFrame([{"Order": o["order_id"], "Machine": o["machine_id"], "Customer": names.get(o["customer_id"], o["customer_id"]),
                                "Product": o["product_id"], "Qty": o["qty"], "Value": float(o["order_value_usd"]), "Priority": o["priority"],
                                "Start": str(o["start_date"])[:10], "Due": str(o["due_date"])[:10], "Status": o["status"],
                                "Penalty per day late": float(o["late_penalty_usd_per_day"])} for o in orders]),
                 hide_index=True, width="stretch", height=min(38 + 35 * len(orders), 360),
                 column_config={"Value": st.column_config.NumberColumn(format="$%.0f"),
                                "Penalty per day late": st.column_config.NumberColumn(format="$%.0f")})

# ------------------------------------------------------------------ OEE
with K.card("bi_oee"):
    hist = [h for h in p["oee"] if h["oee"] is not None]
    st.html(K.card_title("Overall equipment effectiveness", "From the production, maintenance and inspection history: availability "
                         "(maintenance downtime), performance (units made vs. planned) and quality (inspection pass rate)", "gauge"))
    if hist:
        rows = sorted(hist, key=lambda h: h["oee"])
        st.html(K.table([("m", "Machine"), ("line", "Line"), ("oee", "OEE"), ("a", "Availability"), ("p", "Performance"), ("q", "Quality"),
                         ("loss", "Hours lost")], rows, {
            "m": lambda h: f'<b style="color:#1B1816">{K.esc(h["machine_id"])}</b> {K.esc(h["name"])}',
            "oee": lambda h: f'<div style="display:flex;align-items:center;gap:8px;min-width:150px"><div style="flex:1">{K.bar(h["oee"] * 100, h["oee"] < 0.9, label="OEE")}</div>'
                             f'<b class="tabular" style="color:#1B1816">{h["oee"] * 100:.1f}%</b></div>',
            "a": lambda h: f'{h["availability"] * 100:.1f}%', "p": lambda h: f'{h["performance"] * 100:.1f}%',
            "q": lambda h: f'{h["quality"] * 100:.1f}%',
            "loss": lambda h: f'{h["loss_availability_h"] + h["loss_performance_h"] + h["loss_quality_h"]:.0f} h'}))
        loss = pd.DataFrame([{"Machine": h["machine_id"], "Loss": k, "Hours": h[f"loss_{c}_h"]} for h in rows
                             for k, c in (("Downtime", "availability"), ("Slow / short runs", "performance"), ("Rejects", "quality"))])
        chart = alt.Chart(loss).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
            x=alt.X("Machine:N", sort=[h["machine_id"] for h in rows], title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Hours:Q", title="Hours lost", stack="zero"),
            color=alt.Color("Loss:N", scale=alt.Scale(domain=["Downtime", "Slow / short runs", "Rejects"],
                                                      range=[K.ACCENT, K.ACCENT_45, K.NEUTRAL_SOFT]), legend=alt.Legend(title=None)),
            tooltip=["Machine", "Loss", alt.Tooltip("Hours:Q", format=".1f")])
        K.chart(chart, 240)
    unm = [h for h in p["oee"] if h["oee"] is None]
    if unm:
        avail = ", ".join(f"{h['machine_id']} {h['availability'] * 100:.1f}%" for h in unm)
        st.caption(f"No production history yet for these machines, so availability only: {avail}.")
