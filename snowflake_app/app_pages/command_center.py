import altair as alt
import pandas as pd
import streamlit as st

from snowflake_app.core import data as D
from snowflake_app.services import capa_ops, impact_ops
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/command_center.py"
shell.topbar(PATH)


@st.cache_data(ttl=900, show_spinner="Loading plant data…")
def overview() -> dict:
    return D.overview()


data = overview()
k = data["kpis"]
mon = shell.monitor()


def investigate(source: str, machine_id: str | None):
    shell.launch_investigation(source, machine_id)


def open_page(path: str, **state):
    st.session_state.update(state)
    shell.goto(path)


# --------------------------------------------------------------- live-aware top section
@st.fragment(run_every=2 if mon.active else None)
def live_section():
    shell.follow_nav()
    snap = mon.snapshot() if mon.engine.session_id else {"active": False, "machines": [], "events": []}
    live = {m["machine_id"]: m for m in snap.get("machines", [])} if snap.get("active") else {}
    critical = [m for m in live.values() if m["status"] == "critical"]
    downs = [m for m in live.values() if m["status"] == "down"]
    top = data["alerts"][0] if data["alerts"] else None
    try:
        biz = impact_ops.plant(mon.outlook(), mon.work_orders(), shell.settings())
    except Exception:
        biz = None

    # Priority action
    if downs:
        m = downs[0]
        sc = (((biz or {}).get("by_id") or {}).get(m["machine_id"]) or {}).get("scenario") or {}
        risk = (f' · <b style="color:#B54111">{K.count(sc["late_count"], "customer order")} late ({K.usd(sc["value_at_risk"])})</b>'
                if sc.get("late_count") else (f' · {K.usd(sc["cost_per_h"]["total_h"])} per hour down' if sc else ""))
        with K.card("banner", accent=True):
            c1, c2 = st.columns([5, 2], vertical_alignment="center")
            more = f" (+{len(downs) - 1} more)" if len(downs) > 1 else ""
            c1.html(f'<div class="sx-row" style="gap:14px"><div style="width:40px;height:40px;border-radius:10px;background:#F26B1D;color:#FFFFFF;display:flex;align-items:center;justify-content:center;flex:none">{K.icon("wrench", 20)}</div>'
                    f'<div><div class="sx-label sx-accent">Machine down{more}</div>'
                    f'<div style="font-size:14px;margin-top:2px"><b>{K.esc(m["machine_id"])} {K.esc(m["name"])}: {K.esc(K.LIFECYCLE.get(m["lifecycle"], "down").lower())}</b>'
                    f'<span class="sx-muted"> · {K.esc(m.get("work_order_id") or "no work order yet")}</span>{risk}</div></div></div>')
            with c2.container(horizontal=True, horizontal_alignment="right"):
                st.button("Maintenance", icon=":material/build:", type="primary", key="cc_maint", on_click=open_page,
                          args=("app_pages/maintenance.py",))
    elif critical or top:
        with K.card("banner", accent=True):
            c1, c2 = st.columns([5, 2], vertical_alignment="center")
            with c1:
                if critical:
                    m = critical[0]
                    rul = (m.get("rul") or {}).get("hours")
                    tail = f"about {K.duration(rul)} to failure" if rul is not None and rul > 0 else "critical limit breached"
                    st.html(f'<div class="sx-row" style="gap:14px"><div class="sx-banner-ic" style="width:40px;height:40px;border-radius:10px;background:#F26B1D;color:#FFFFFF;display:flex;align-items:center;justify-content:center;flex:none">{K.icon("shield", 20)}</div>'
                            f'<div><div class="sx-label sx-accent">Live alert · {K.esc(K.ts(snap.get("sim_time")))}</div>'
                            f'<div style="font-size:14px;margin-top:2px"><b>{K.esc(m["machine_id"])} {K.esc(m["name"])} is critical</b>'
                            f'<span class="sx-muted"> · health {K.num(m["health"])}% · {K.esc(tail)}</span></div></div></div>')
                else:
                    fleet_def = next((f["defects"] for f in data["fleet"] if f["machine_id"] == top["machine_id"]), 0)
                    st.html(f'<div class="sx-row" style="gap:14px"><div style="width:40px;height:40px;border-radius:10px;background:#F26B1D;color:#FFFFFF;display:flex;align-items:center;justify-content:center;flex:none">{K.icon("shield", 20)}</div>'
                            f'<div><div class="sx-label sx-accent">Highest risk asset</div>'
                            f'<div style="font-size:14px;margin-top:2px"><b>{K.esc(top["machine_id"])} · {K.sensor_label(top["sensor"])} '
                            f'{K.sensor_val(top["sensor"], top["current_value"])} {K.sensor_unit(top["sensor"])}</b>'
                            f'<span class="sx-muted"> exceeds the {K.esc(top["critical_threshold"])} {K.sensor_unit(top["sensor"])} critical limit · '
                            f'machine produces {fleet_def} of {k["defects"]} defects</span></div></div></div>')
            with c2:
                with st.container(horizontal=True, horizontal_alignment="right"):
                    st.button("Telemetry", icon=":material/monitor_heart:", key="cc_tel", on_click=open_page,
                              args=("app_pages/live_monitoring.py",),
                              kwargs={"focus_machine": critical[0]["machine_id"] if critical else None})
                    busy = shell.run_manager().active() is not None
                    st.button("Investigation running" if busy else "Investigate with 8D", icon=":material/account_tree:",
                              type="primary", disabled=busy, key="cc_inv", on_click=investigate,
                              args=("live", critical[0]["machine_id"]) if critical else ("auto", top["machine_id"]))

    # KPIs
    at_risk = len(critical) or k["machines_at_risk"]
    flagged = sum(1 for m in live.values() if m["status"] not in ("healthy", "learning", "down"))
    hint = (f"{flagged} flagged live · {len(downs)} down" if downs else f"{flagged} flagged live") if live else "From sensor trend scan"
    tiles = [
        K.kpi("Defects", K.num(k["defects"]), hint=f"{k['critical_defects']} critical · last 13 weeks", spark=data["spark"]["defects"], ico="package"),
        K.kpi("First-pass yield", K.num(k["first_pass_yield_pct"], 2), "%", f"{K.num(k['inspections'])} inspections",
              spark=[100 - v for v in data["spark"]["fail_rate"]], ico="gauge"),
        K.kpi("Fail rate", K.num(k["fail_rate_pct"], 2), "%", "Inspection FAIL share", spark=data["spark"]["fail_rate"], ico="percent"),
        K.kpi("Machines at risk", str(at_risk), f"/ {k['machines_total']}", hint,
              ico="factory", emphasis=at_risk > 0),
        K.kpi("Open containment", K.num(k["open_containment"]), hint="Defects without closed containment", ico="shield",
              emphasis=k["open_containment"] > 10),
        K.kpi("Est. cost of poor quality", K.usd(k["copq_estimate"]), hint="Scrap, rework, warranty, engineering", ico="dollar"),
    ]
    st.html(f'<section class="sx-kpis" aria-label="Key performance indicators">{"".join(tiles)}</section>')
    actions_needed(biz)

    left, right = st.columns([8, 4])
    with left:
        plant_floor(live)
    with right:
        monitoring_summary(snap)


def actions_needed(biz: dict | None):
    """Follow-up across the business modules: overdue corrective actions, parts to order, customer orders at risk."""
    ck = capa_ops.kpis()
    to_order = shell.parts_to_order()
    bk = (biz or {}).get("kpis") or {}
    late = bk.get("late_now", 0) + bk.get("late_forecast", 0)
    value = bk.get("value_now", 0) + bk.get("value_forecast", 0)
    items = [("cc_ca", "app_pages/corrective_actions.py", "task", "Corrective actions",
              f"{ck['overdue']} overdue" if ck["overdue"] else f"{ck['open']} open", ck["overdue"] > 0,
              f"{ck['due_week']} due this week · {ck['total']} tracked"),
             ("cc_sp", "app_pages/spare_parts.py", "box", "Spare parts", f"{to_order} to order" if to_order else "Stock OK", to_order > 0,
              "At or below the reorder point after reservations"),
             ("cc_bi", "app_pages/business_impact.py", "dollar", "Customer orders", f"{late} at risk" if late else "On schedule", late > 0,
              f"{K.usd(value)} of orders" if late else f"{bk.get('open_orders', 0)} open orders · {K.usd(bk.get('open_value'))}")]
    cols = st.columns(3)
    for col, (key, path, ico, title, val, hot, hint) in zip(cols, items):
        with col, st.container(border=True, key=f"{'cardaccent' if hot else 'card'}_{key}"):
            c1, c2 = st.columns([3, 2], vertical_alignment="center")
            c1.html(f'<div class="sx-row" style="gap:8px"><span class="sx-subtle">{K.icon(ico, 16)}</span><span class="sx-label">{K.esc(title)}</span></div>'
                    f'<div style="font-size:17px;font-weight:600;margin-top:2px;color:{"#B54111" if hot else "#1B1816"}">{K.esc(val)}</div>'
                    f'<div class="sx-subtle" style="font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(hint)}</div>')
            c2.button("Open", key=f"{key}_open", icon=":material/arrow_forward:", icon_position="right", type="tertiary",
                      on_click=open_page, args=(path,))


def plant_floor(live: dict):
    lines: dict[str, list] = {}
    for m in data["fleet"]:
        lines.setdefault(m["line"], []).append(m)
    legend = "".join(K.pill(s, l, "xs") for s, l in (("critical", None), ("warning", None), ("drift", None), ("healthy", "OK")))
    rows = []
    for line, ms in sorted(lines.items()):
        tiles = []
        for m in ms:
            lv = live.get(m["machine_id"])
            status = (lv["lifecycle"] if lv["status"] == "down" else lv["status"]) if lv else m["risk"]
            hot = status in ("critical", "high", "warning", "tripped", "stopped", "in_repair")
            border = "#F26B1D" if status in ("critical", "tripped") else ("rgba(242,107,29,.4)" if hot else "#E5E2DE")
            bg = "rgba(242,107,29,.06)" if status in ("critical", "tripped") else "#FFFFFF"
            health = f' · health {lv["health"]:.0f}%' if lv and lv.get("health") is not None else ""
            live_dot = f'<span style="margin-left:auto">{K.dot(True)}</span>' if lv else ""
            n = m["defects"]
            tiles.append(
                f'<li style="list-style:none;border:1px solid {border};background:{bg};border-radius:10px;padding:9px 10px;min-width:0" '
                f'aria-label="{K.esc(m["machine_id"])} {K.esc(m["name"])}, {K.esc(status)}, {n} defects">'
                f'<div class="sx-row" style="justify-content:space-between"><span class="sx-mono" style="font-size:12px;font-weight:500;white-space:nowrap">{K.esc(m["machine_id"])}</span>'
                f'{K.pill(status, "OK" if status == "low" else None, "xs")}</div>'
                f'<div class="sx-muted" style="font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(m["name"])}</div>'
                f'<div class="sx-row sx-subtle tabular" style="font-size:11px;margin-top:4px;white-space:nowrap">{n} {"defect" if n == 1 else "defects"}{health}{live_dot}</div></li>')
        rows.append(
            f'<div style="display:grid;grid-template-columns:76px 1fr;gap:10px;align-items:stretch">'
            f'<div style="background:#F5F4F2;border-radius:10px;padding:8px 10px;display:flex;flex-direction:column;justify-content:center">'
            f'<span style="font-size:12px;font-weight:600">{K.esc(line.replace("Line-", "Line "))}</span><span class="sx-subtle" style="font-size:11px">{len(ms)} assets</span></div>'
            f'<ul style="display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:8px;margin:0;padding:0">{"".join(tiles)}</ul></div>')
    sub = ("Live status from the online model; others from historical sensor trends" if live
           else "Status from historical sensor trend scan")
    with K.card("plant"):
        st.html(K.card_title("Plant floor", sub, "factory", legend) + f'<div style="display:grid;gap:9px">{"".join(rows)}</div>')


def monitoring_summary(snap: dict):
    with K.card("monsum"):
        if not snap.get("active"):
            st.html(K.card_title("Live monitoring", None, "activity") +
                    f'<div style="border:1px solid #E5E2DE;border-radius:10px;padding:28px 18px;text-align:center;'
                    f'background-image:linear-gradient(rgba(229,226,222,.5) 1px,transparent 1px),linear-gradient(90deg,rgba(229,226,222,.5) 1px,transparent 1px);background-size:24px 24px">'
                    f'<div style="width:44px;height:44px;border-radius:12px;background:#FFF1E8;color:#B54111;display:inline-flex;align-items:center;justify-content:center">{K.icon("cpu", 20)}</div>'
                    f'<div style="font-weight:600;margin-top:10px">No monitoring session</div>'
                    f'<div class="sx-subtle" style="font-size:12px;max-width:290px;margin:4px auto 0">Stream telemetry for 10 sensor-equipped machines. '
                    f'The model learns each machine\'s normal pattern, warns early, forecasts time to failure and raises work orders.</div></div>')
            st.button("Set up monitoring", icon=":material/play_arrow:", type="primary", key="cc_setup", width="stretch",
                      on_click=open_page, args=("app_pages/live_monitoring.py",))
            return
        counts = {s: sum(1 for m in snap["machines"] if m["status"] == s) for s in ("down", "critical", "warning", "healthy")}
        st.html(K.card_title("Live monitoring", f"Session {snap['session_id']} · {K.ts(snap['sim_time'])}", "activity"))
        cells = "".join(
            f'<div style="background:#F5F4F2;border-radius:10px;padding:8px 0;text-align:center">'
            f'<div class="tabular" style="font-size:18px;font-weight:600;color:{"#B54111" if s in ("down", "critical", "warning") and counts[s] else "#1B1816"}">{counts[s]}</div>'
            f'<div class="sx-subtle" style="font-size:11px;text-transform:capitalize">{s}</div></div>' for s in counts)
        st.html(f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">{cells}</div>')
        open_wo = mon.plant_kpis()["open"]
        if open_wo:
            rows = "".join(f'<li style="list-style:none;display:flex;gap:8px;align-items:center;font-size:12px">{K.pill(w["priority"], size="xs")}'
                           f'<span class="sx-mono">{K.esc(w["machine_id"])}</span><span class="sx-muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                           f'{K.esc(w["component"])} · {K.esc(w["status"].replace("_", " "))}</span></li>'
                           for w in sorted(open_wo, key=lambda w: w["priority"])[:3])
            st.html(f'<div class="sx-label" style="margin-top:12px">Actions needed · {len(open_wo)} open work orders</div>'
                    f'<ul style="margin:6px 0 0;padding:0;display:grid;gap:6px">{rows}</ul>')
            st.button("Open maintenance", icon=":material/build:", key="cc_openmaint", type="tertiary",
                      on_click=open_page, args=("app_pages/maintenance.py",))
        items = []
        for e in list(reversed(snap["events"]))[:5]:
            c = "#F26B1D" if e["level"] == "critical" else ("#F8B48C" if e["level"] == "warning" else "#A8A29E")
            items.append(f'<li style="display:flex;gap:8px;font-size:12px;line-height:17px"><span style="margin-top:6px;width:6px;height:6px;border-radius:9px;background:{c};flex:none"></span>'
                         f'<span class="sx-muted" style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">'
                         f'<span class="sx-mono" style="color:#1B1816">{K.esc(e["machine_id"] or "Model")}</span> {K.esc(e["message"])}</span></li>')
        st.html(f'<ol style="margin:12px 0 0;padding:0;display:grid;gap:8px" aria-label="Latest monitoring events">{"".join(items)}</ol>')
        st.button("Open live monitoring", icon=":material/arrow_forward:", key="cc_openmon", type="tertiary",
                  on_click=open_page, args=("app_pages/live_monitoring.py",))


live_section()

# ------------------------------------------------------------------ charts
c1, c2 = st.columns([7, 5])
with c1, K.card("trend"):
    st.html(K.card_title("Defects by week", "Split by severity", "package"))
    tr = pd.DataFrame(data["trend"]).melt(id_vars="week", value_vars=["Critical", "Major", "Minor"], var_name="Severity", value_name="Defects")
    tr["order"] = tr["Severity"].map({"Critical": 0, "Major": 1, "Minor": 2})
    bars = alt.Chart(tr).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        x=alt.X("week:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0, tickCount=7)),
        y=alt.Y("Defects:Q", title=None, stack="zero", axis=alt.Axis(tickMinStep=1)),
        color=alt.Color("Severity:N", scale=alt.Scale(domain=["Critical", "Major", "Minor"], range=[K.ACCENT, K.ACCENT_45, K.NEUTRAL_SOFT]),
                        legend=alt.Legend(title=None, orient="bottom")),
        order=alt.Order("order:Q", sort="ascending"),
        tooltip=[alt.Tooltip("week:T", title="Week of", format="%b %d, %Y"), "Severity", "Defects"])
    K.chart(bars, 250)

with c2, K.card("pareto"):
    st.html(K.card_title("Defect Pareto", "Where to focus: top types and cumulative share", "layers"))
    pr = pd.DataFrame(data["pareto"])
    base = alt.Chart(pr).encode(x=alt.X("type:N", sort=None, title=None,
                                        axis=alt.Axis(labelAngle=0, labelExpr="split(datum.label, ' ')", labelLimit=90)))
    b = base.mark_bar(color=K.NEUTRAL_SOFT, cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        y=alt.Y("count:Q", title="Defects", axis=alt.Axis(tickMinStep=1)), tooltip=["type", "count", "cum_pct"])
    ln = base.mark_line(color=K.ACCENT, strokeWidth=2, point=alt.OverlayMarkDef(color=K.ACCENT, size=36)).encode(
        y=alt.Y("cum_pct:Q", title="Cumulative %", scale=alt.Scale(domain=[0, 100])), tooltip=["type", "cum_pct"])
    K.chart(alt.layer(b, ln).resolve_scale(y="independent"), 250)

c1, c2, c3 = st.columns(3)
with c1, K.card("shift"):
    rows = data["shifts"]
    mx = max(r["defects_per_100_orders"] for r in rows) or 1
    items = "".join(
        f'<li style="list-style:none"><div class="sx-row" style="justify-content:space-between;font-size:12px;margin-bottom:5px">'
        f'<span style="font-weight:500">{K.esc(r["shift"])}</span><span class="sx-muted tabular">{r["defects"]} defects / {r["orders"]} orders · '
        f'<b style="color:#1B1816">{r["defects_per_100_orders"]}</b></span></div>'
        f'{K.bar(r["defects_per_100_orders"] / mx * 100, r["defects_per_100_orders"] == mx, label=r["shift"] + " defects per 100 orders")}</li>'
        for r in rows)
    st.html(K.card_title("Shift quality", "Defects per 100 production orders", "users") + f'<ul style="margin:0;padding:0;display:grid;gap:14px">{items}</ul>')

with c2, K.card("suppliers"):
    rows = data["suppliers"]
    mx = max([r["defective_orders"] for r in rows] + [1])
    items = "".join(
        f'<li style="list-style:none;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center">'
        f'<div style="min-width:0"><div style="font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(r["name"])} '
        f'<span class="sx-mono sx-subtle">{K.esc(r["supplier_id"])}</span></div>'
        f'<div style="margin-top:5px">{K.bar(r["defective_orders"] / mx * 100, i == 0, label=r["name"] + " exposure")}</div></div>'
        f'<span class="sx-muted tabular" style="font-size:12px">{r["defective_orders"]} orders · {r["lots"]} lots</span></li>'
        for i, r in enumerate(rows))
    st.html(K.card_title("Supplier exposure", "Defective orders consuming each supplier's lots", "truck") + f'<ul style="margin:0;padding:0;display:grid;gap:11px">{items}</ul>')

with c3, K.card("latest"):
    runs = shell.run_manager().list()
    r = runs[0] if runs else None
    st.html(K.card_title("Latest 8D investigation", None, "branch"))
    if not r:
        st.html('<div style="text-align:center;padding:12px 0 6px"><div style="font-weight:600">No investigations yet</div>'
                '<div class="sx-subtle" style="font-size:12px;margin-top:3px">Launch the 11-agent pipeline against the highest-risk machine.</div></div>')
        top = data["alerts"][0]["machine_id"] if data["alerts"] else None
        st.button("Run first 8D", icon=":material/play_arrow:", type="primary", key="cc_first", width="stretch",
                  on_click=investigate, args=("auto", top))
    else:
        t = r["trigger"]
        pct = r["progress"]["done"] / r["progress"]["total"] * 100
        st.html(f'<div class="sx-row" style="justify-content:space-between"><span class="sx-mono" style="font-size:12px">{K.esc(r["id"])}</span>{K.pill(r["status"], size="xs")}</div>'
                f'<div style="font-size:13.5px;margin:8px 0 10px">{K.esc(t.get("machine_id"))} {K.esc(t.get("machine_name"))} <span class="sx-muted">· {K.esc(t.get("line"))}</span></div>'
                f'{K.bar(pct, True, r["status"] == "running", "Investigation progress")}'
                f'<div class="sx-subtle" style="font-size:12px;margin-top:8px">{r["progress"]["done"]} of {r["progress"]["total"]} agents · '
                f'{K.num(r.get("total_tokens"))} tokens · {K.esc(r["provider"].get("model"))}</div>')
        st.button("Open investigation", icon=":material/arrow_forward:", key="cc_openinv", type="tertiary",
                  on_click=open_page, args=("app_pages/investigations.py",), kwargs={"selected_run": r["id"]})

with K.card("recent"):
    st.html(K.card_title("Recent defects", "Most recent first · select a row to trace it", "package"))
    df = pd.DataFrame(data["recent_defects"])[["defect_id", "timestamp", "defect_type", "severity", "machine_id", "line",
                                                "shift", "characteristic", "containment_status"]]

    def _trace():
        rows = st.session_state.cc_defects.selection.rows
        if rows:
            open_page("app_pages/traceability.py", trace_defect=df.iloc[rows[0]]["defect_id"])

    st.dataframe(df, hide_index=True, on_select=_trace, selection_mode="single-row", key="cc_defects",
                 column_config={"defect_id": "Defect", "timestamp": "Date", "defect_type": "Type", "severity": "Severity",
                                "machine_id": "Machine", "line": "Line", "shift": "Shift", "characteristic": "Characteristic",
                                "containment_status": "Containment"})
