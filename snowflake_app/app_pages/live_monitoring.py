
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from snowflake_app.core.monitoring import SENSORS, SENSOR_LABEL, SENSOR_UNIT
from snowflake_app.services import impact_ops
from snowflake_app.tools.prediction_engine import LIVE_THRESHOLDS as SENSOR_THRESHOLDS
from snowflake_app.ui import chat_dock, kit as K, maint, shell

PATH = "app_pages/live_monitoring.py"
shell.topbar(PATH)

mon = shell.monitor()
ss = st.session_state
ss.setdefault("follow_risk", shell.settings()["display"]["follow_risk"])
ss.setdefault("mon_sensor", "auto")
ss.setdefault("mon_range", "live")
ss.setdefault("event_filter", "All")
STATUS_RANK = {"down": 0, "critical": 1, "warning": 2, "drift": 3, "healthy": 4, "learning": 5}
synced = shell.synced
INTERVALS = [1, 2, 5]
RANGES = {"live": "Live", "24h": "24 h", "7d": "7 days"}
ENG = K.engineer()
# six signals share one strip: short labels there, the full name on hover
STRIP_LABEL = {"vibration_mm_s": "Vibration", "temp_c": "Temp", "spindle_rpm": "Speed", "pressure_bar": "Pressure",
               "power_kw": "Power", "coolant_flow_lpm": "Coolant"}


# =============================================================================
# Setup (no active session)
# =============================================================================

def setup_view():
    snap = mon.snapshot()
    defaults = snap["defaults"]
    auto = shell.settings()["automation"]
    if snap.get("session_id"):
        st.info(f"Monitoring stopped at {K.ts(snap['sim_time'])} after {K.num(snap['tick'])} samples. "
                "Start a new session to resume the live stream.", icon=":material/info:")
    k = mon.plant_kpis()
    if k["down"] or k["open"]:
        st.html(f'<div class="sx-inset" style="padding:10px 12px;font-size:12.5px;margin-bottom:8px">{K.icon("wrench", 14)} '
                f'<b>Plant state is kept between sessions.</b> {len(k["down"])} machine(s) down'
                f'{" (" + ", ".join(k["down"]) + ")" if k["down"] else ""} and {len(k["open"])} open work order(s). '
                f'Down machines stay offline until their repair is completed on the Maintenance page.</div>')
    lines: dict[str, list] = {}
    for m in defaults["fleet"]:
        lines.setdefault(m["line"], []).append(m)

    with st.form("mon_setup", border=False):
        left, right = st.columns([7, 5])
        with left:
            with K.card("scope"):
                st.html(K.card_title("Monitoring scope", "Machines with sensor telemetry, grouped by production line", "factory"))
                for line, ms in sorted(lines.items()):
                    st.pills(line.replace("Line-", "Line "), [m["machine_id"] for m in ms], selection_mode="multi",
                             default=[m["machine_id"] for m in ms], key=f"scope_{line}",
                             format_func=lambda mid, ms=ms: f"{mid} · {next(x['name'] for x in ms if x['machine_id'] == mid)}")
            with K.card("stream"):
                st.html(K.card_title("Telemetry stream", "Every sample is time-stamped on the plant clock as it arrives", "activity"))
                c1, c2 = st.columns(2)
                c1.segmented_control("Sampling interval", INTERVALS, default=1, key="cfg_interval", format_func=lambda v: f"{v} s",
                                     help="How often each machine reports all six signals.")
                c2.slider("Normal-pattern window (samples)", 30, 180, 60, 10, key="cfg_train",
                          help="How many samples from each machine's normal period the model learns from, and how long "
                               "a repaired machine runs in commissioning before alerts are switched back on.")
                st.toggle("Include a machine close to failure", value=auto.get("near_failure_machine", True), key="cfg_near",
                          help="If no machine is within a few hours of failure, one healthy machine starts a fast-developing "
                               "fault, so the full stop, repair and return-to-service cycle can be followed in one session.")
        with right:
            with K.card("model"):
                st.html(K.card_title("Early-warning model", "How sensitive the alerts are", "cpu"))
                st.segmented_control("Alert sensitivity", ["low", "medium", "high"], default="medium", key="cfg_sens",
                                     format_func=str.capitalize,
                                     help="High raises alerts earlier with more false alarms. "
                                          "(Hotelling T² false-alarm rate α: low 0.0001, medium 0.001, high 0.01.)")
                st.slider("Change-check window (samples)", 20, 80, 40, 5, key="cfg_window",
                          help="Recent samples compared with the learned normal to spot a behaviour change (PSI).")
                st.slider("Change sensitivity floor", 0.10, 0.50, 0.25, 0.05, key="cfg_psi",
                          help="Minimum population stability index (PSI) that counts as a behaviour change.")
            with K.card("pipeline"):
                steps = [("Learn normal", "Each machine's normal pattern is learned from its most recent normal period."),
                         ("Spot unusual behaviour", "Every sample is compared with that pattern across all six signals at once."),
                         ("Detect change", "A signal that settles somewhere new is flagged as a behaviour change."),
                         ("Wear or process change?", "A steady trend means wear (forecast it); a one-off step means a process change (re-learn normal)."),
                         ("Forecast and act", "Time to failure with a likely range; maintenance is planned before the limit, "
                                              "and a machine that crosses it stops automatically.")]
                items = "".join(f'<li style="display:grid;grid-template-columns:22px 1fr;gap:10px;list-style:none">'
                                f'<span style="width:22px;height:22px;border-radius:7px;background:#FFF1E8;color:#B54111;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center">{i}</span>'
                                f'<div><div style="font-size:13px;font-weight:600">{t}</div><div class="sx-subtle" style="font-size:12px">{d}</div></div></li>'
                                for i, (t, d) in enumerate(steps, 1))
                st.html(K.card_title("What runs on every sample", None, "layers") + f'<ol style="margin:0;padding:0;display:grid;gap:10px">{items}</ol>')
            go = st.form_submit_button("Start monitoring", type="primary", icon=":material/play_arrow:", width="stretch")

    if go:
        machines = [mid for line in lines for mid in (ss.get(f"scope_{line}") or [])]
        if not machines:
            st.error("Select at least one machine to monitor.", icon=":material/error:")
            return
        settings = shell.settings()
        settings["automation"]["near_failure_machine"] = bool(ss.cfg_near)
        shell.save_settings()
        mon.start({"clock": "realtime", "sample_interval_s": ss.cfg_interval or 1, "machines": machines,
                   "training_ticks": ss.cfg_train, "sensitivity": ss.cfg_sens or "medium", "drift_window": ss.cfg_window,
                   "psi_threshold": ss.cfg_psi}, settings)
        ss.focus_machine = None
        ss.pop("live_frozen", None)
        st.rerun()


# =============================================================================
# Live view
# =============================================================================

def _pick_focus(machines: list[dict]) -> dict:
    by_id = {m["machine_id"]: m for m in machines}
    if not ss.follow_risk and ss.focus_machine in by_id:
        return by_id[ss.focus_machine]
    live = [m for m in machines if m["status"] != "down"] or machines
    return sorted(live, key=lambda m: (STATUS_RANK.get(m["status"], 6), m["health"] if m["health"] is not None else 100))[0]


def _on_pick():
    v = ss.get("pick_machine")
    if v:
        ss.focus_machine = v
        ss.follow_risk = False
        ss.w_follow_risk = False


def _freeze(on: bool):
    if on:
        ss.live_frozen = mon.snapshot()
    else:
        ss.pop("live_frozen", None)


def _interval(snap: dict) -> float:
    return snap["config"].get("sample_interval_s") or snap["config"]["minutes_per_tick"] * 60


def session_strip(snap: dict):
    cfg = snap["config"]
    frozen = "live_frozen" in ss
    if frozen:
        state = K.pill("pending", "View frozen")
    elif snap["paused"]:
        state = K.pill("pending", "Paused")
    else:
        state = f'<span class="sx-pill crit">{K.dot(True)}LIVE</span>'
    uptime = time.time() - snap["started_at"] if snap.get("started_at") else None
    down = sum(1 for m in snap["machines"] if m["status"] == "down")
    running = len(snap["machines"]) - down
    stats = [("Last sample", K.ts(snap["sim_time"])), ("Monitoring for", K.seconds(uptime)), ("Samples", K.num(snap["tick"])),
             ("Sampling", f"every {_interval(snap):g} s"), ("Running", f"{running} of {len(snap['machines'])}")]
    if ENG:
        stats.append(("Anomaly limit", f"{cfg['score_threshold']}σ"))
    cells = "".join(f'<div><div class="sx-label">{k}</div><div class="tabular" style="font-size:13px;font-weight:500;margin-top:1px">{v}</div></div>' for k, v in stats)
    with K.card("strip"):
        c1, c2 = st.columns([8, 3], vertical_alignment="center")
        c1.html(f'<div class="sx-row" style="gap:22px;flex-wrap:wrap">{state}<div><div style="font-weight:600">Session {K.esc(snap["session_id"])}</div>'
                f'<div class="sx-subtle" style="font-size:12px">{len(snap["machines"])} machines · {running * len(SENSORS)} live signals</div></div>{cells}</div>')
        with c2.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center"):
            st.button("Resume live" if frozen else "Freeze view", icon=":material/play_arrow:" if frozen else ":material/pause:",
                      key="live_freeze", on_click=_freeze, args=(not frozen,),
                      help="Hold the charts still for discussion. Telemetry keeps streaming and scoring in the background.")
            st.button("Stop", icon=":material/stop:", key="live_stop", on_click=mon.stop,
                      help="Stops the stream. Machine conditions, down machines and work orders are kept.")


def alert_banner(snap: dict):
    alerts = [a for a in snap["alerts"]]
    if not alerts:
        return
    a = sorted(alerts, key=lambda x: x["tick"])[-1]
    m = next((x for x in snap["machines"] if x["machine_id"] == a["machine_id"]), {})
    busy = shell.run_manager().active() is not None
    with K.card("alert", accent=True):
        c1, c2 = st.columns([5, 2], vertical_alignment="center")
        if m.get("status") == "down":
            wo = m.get("work_order_id")
            head = f"{a['machine_id']} {a['name']} stopped automatically at {K.ts(a.get('down_since') or a['sim_time'])}"
            sub = (f"{K.sensor_label(a['sensor'])} held above its critical limit · machine is down"
                   + (f" · work order {wo} raised" if wo else "") + f" · {a['line']}")
        else:
            rul = f" · about {K.duration(a['rul_hours'])} to failure" if a.get("rul_hours") is not None else ""
            head = f"{a['machine_id']} {a['name']} reached critical at {K.ts(a['sim_time'])}"
            sub = f"{K.sensor_label(a['sensor'])} {K.sensor_val(a['sensor'], a['value'])} {K.sensor_unit(a['sensor'])}{rul} · {a['line']}"
        c1.html(f'<div class="sx-row" style="gap:12px"><span class="sx-accent">{K.icon("wrench" if m.get("status") == "down" else "alert", 20)}</span><div>'
                f'<div style="font-weight:600">{K.esc(head)}</div><div class="sx-muted" style="font-size:12px">{K.esc(sub)}</div></div></div>')
        with c2.container(horizontal=True, horizontal_alignment="right"):
            if m.get("status") == "down":
                st.button("Maintenance", icon=":material/build:", key="live_maint", on_click=shell.goto,
                          args=("app_pages/maintenance.py",))
            st.button("Investigation running" if busy else "Launch 8D", icon=":material/account_tree:", type="primary",
                      key="live_launch", disabled=busy, on_click=shell.launch_investigation, args=("live", a["machine_id"]))


def fleet_grid(snap: dict, focus: dict):
    cards = []
    now = time.time()
    for m in snap["machines"]:
        hist = snap["history"].get(m["machine_id"], [])
        scores = [h["score"] for h in hist[-60:] if h.get("score") is not None]
        sel = m["machine_id"] == focus["machine_id"]
        crit = m["status"] == "critical"
        down = m["status"] == "down"
        border = "#F26B1D" if (sel or crit or (down and m["lifecycle"] == "tripped")) else "#E5E2DE"
        shadow = "box-shadow:0 0 0 3px rgba(242,107,29,.18);" if sel else ""
        bg = "#F5F4F2" if down else "#fff"
        if down:
            since = (now - m["down_since_ms"] / 1000) if m.get("down_since_ms") else None
            word = {"tripped": "Down", "stopped": "Stopped", "in_repair": "In repair"}.get(m["lifecycle"], "Down")
            val = (f'<div class="sx-row" style="gap:6px;margin-top:8px;color:#B54111">{K.icon("wrench", 16)}'
                   f'<span class="tabular" style="font-size:15px;font-weight:600">{word} {K.seconds(since)}</span></div>'
                   f'<div class="sx-subtle sx-mono" style="font-size:11px;margin-top:2px">{K.esc(m.get("work_order_id") or "No work order")}</div>')
            barh = ""
        elif m["health"] is None:
            prog = m.get("learning_progress") or 0
            label = "Commissioning" if m.get("lifecycle") == "commissioning" else "Learning normal"
            val = f'<div class="sx-subtle" style="font-size:12px;margin-top:6px">{label} · {prog:.0f}%</div>'
            barh = K.bar(prog, True, True, f"{m['machine_id']} learning progress")
        else:
            col = "#B54111" if m["status"] in ("critical", "warning") else "#1B1816"
            act = m.get("action")
            act_html = f'<div style="margin-top:4px">{K.pill(act, size="xs")}</div>' if act in ("plan", "urgent") else ""
            val = (f'<div class="sx-label" style="margin-top:6px">Health</div><div class="sx-row" style="align-items:flex-end;gap:10px">'
                   f'<div class="tabular" style="font-size:22px;font-weight:600;color:{col};line-height:26px">{m["health"]:.0f}<small style="font-size:12px;color:#78716B">%</small></div>'
                   f'<div style="flex:1;min-width:0;height:26px">{K.sparkline(scores, 120, 26, False, "#57524D") if len(scores) > 2 else ""}</div></div>')
            barh = (f'<div class="sx-bar" style="height:5px;margin-top:6px" role="progressbar" aria-valuenow="{m["health"]:.0f}" aria-label="{m["machine_id"]} health">'
                    f'<span style="width:{m["health"]:.0f}%;background:{"#F26B1D" if crit else "#57524D"}"></span></div>{act_html}')
        status = m["lifecycle"] if down else m["status"]
        cards.append(f'<div style="border:1px solid {border};{shadow}border-radius:12px;background:{bg};padding:10px 12px;min-width:0">'
                     f'<div class="sx-row" style="justify-content:space-between"><span class="sx-mono" style="font-size:12px;font-weight:500">{K.esc(m["machine_id"])}</span>{K.pill(status, size="xs")}</div>'
                     f'<div class="sx-subtle" style="font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(m["name"])}</div>{val}{barh}</div>')
    st.html(f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(172px,1fr));gap:10px" aria-label="Fleet health">{"".join(cards)}</div>')


def _sensor_for(m: dict) -> str:
    if ss.mon_sensor != "auto":
        return ss.mon_sensor
    if m.get("rul"):
        return m["rul"]["sensor"]
    if m.get("drift"):
        return m["drift"]["sensor"]
    return m.get("top_sensor") or SENSORS[0]


def focus_panel(snap: dict, m: dict):
    mid = m["machine_id"]
    mdl = m["model"]
    sensor = _sensor_for(m)
    hist = snap["history"].get(mid, [])
    th = SENSOR_THRESHOLDS[sensor]
    down = m["status"] == "down"

    with K.card("focus"):
        c1, c2 = st.columns([3, 2], vertical_alignment="center")
        if down:
            sub = f"{m['line']} · {K.esc(K.LIFECYCLE.get(m['lifecycle'], 'Down'))}"
        elif mdl["version"]:
            trained = f"normal learned {K.ts(datetime.fromtimestamp(mdl['trained_ms'] / 1000).astimezone().isoformat(timespec='seconds'))}" if mdl.get("trained_ms") else ""
            sub = f"{m['line']} · model v{mdl['version']} · {trained}"
        else:
            sub = f"{m['line']} · learning its normal pattern"
        c1.html(K.card_title(f"{mid} · {m['name']}", sub, "activity"))
        with c2.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center"):
            st.html(K.pill(m["lifecycle"] if down else m["status"]))
            busy = shell.run_manager().active() is not None
            st.button("", icon=":material/forum:", key="focus_ask", type="tertiary",
                      help="Ask the Plant Assistant about this machine", on_click=chat_dock.ask_about, args=(mid,))
            st.button("Investigate", icon=":material/account_tree:", key="focus_inv",
                      disabled=busy or m["status"] not in ("critical", "warning", "down"),
                      type="primary" if m["status"] in ("critical", "down") else "secondary",
                      help="Launch an 8D investigation from this machine's live state",
                      on_click=shell.launch_investigation, args=("live", mid))

        # sensor strip
        boxes = []
        for s in SENSORS:
            v = m["values"].get(s)
            z = ""
            if mdl["version"] and v is not None and not down:
                sd = mdl["std"][s] or 1e-9
                zz = (v - mdl["mean"][s]) / sd
                txt = f"{zz:+.1f}σ vs normal" if ENG else K.deviation(zz)
                z = f'<div style="font-size:11px;color:{"#B54111" if abs(zz) >= 3 else "#78716B"}">{txt}</div>'
            elif down:
                z = '<div style="font-size:11px;color:#78716B">last reading</div>'
            on = s == sensor
            flag = K.icon("triangle", 12) if (m.get("drift") or {}).get("sensor") == s or (m.get("rul") or {}).get("sensor") == s else ""
            boxes.append(f'<div title="{SENSOR_LABEL[s]}" style="padding:8px 7px;min-width:0;border-bottom:2px solid {"#F26B1D" if on else "transparent"};background:{"#F5F4F2" if on else "transparent"}">'
                         f'<div class="sx-label sx-row" style="gap:3px;white-space:nowrap;overflow:hidden;letter-spacing:.02em;font-size:10.5px;{"color:#B54111" if flag else ""}"><span style="overflow:hidden;text-overflow:ellipsis">{STRIP_LABEL.get(s, SENSOR_LABEL[s])}</span>{flag}</div>'
                         f'<div class="tabular" style="font-size:15px;font-weight:600">{K.sensor_val(s, v)} <small style="font-size:11px;color:#78716B;font-weight:400">{SENSOR_UNIT[s]}</small></div>{z}</div>')
        st.html(f'<div style="display:grid;grid-template-columns:repeat({len(SENSORS)},minmax(0,1fr));border-top:1px solid #E5E2DE;border-bottom:1px solid #E5E2DE;margin:2px -18px 10px">{"".join(boxes)}</div>')
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            synced(st.selectbox, "Chart signal", ["auto"] + SENSORS, state="mon_sensor", label_visibility="collapsed",
                   width=190, format_func=lambda s: "Signal: most at risk" if s == "auto" else f"Signal: {SENSOR_LABEL[s].lower()}")
            st.space("stretch")
            synced(st.segmented_control, "Time range", list(RANGES), state="mon_range", label_visibility="collapsed",
                   format_func=RANGES.get)

        rng = ss.mon_range or "live"
        if rng == "live":
            pts = [{"x": float(h["ms"]), "v": h[sensor], "score": h.get("score")} for h in hist]
        else:
            ser = mon.series(mid, "mid" if rng == "24h" else "long")
            pts = [{"x": float(p["ms"]), "v": p[sensor], "score": None} for p in ser]
            if hist and not down:
                pts += [{"x": float(h["ms"]), "v": h[sensor], "score": None} for h in hist[-1:]]
        if not pts:
            st.caption("Waiting for the first samples…")
            return
        # x is epoch ms on a linear scale with clock-time labels (Streamlit's Vega temporal scale collapses these domains)
        df = pd.DataFrame(pts)
        last_ms = int(df["x"].iloc[-1])
        now_ms = int(time.time() * 1000) if down else last_ms
        span = {"live": max(last_ms - int(df["x"].min()), 60_000), "24h": 24 * 3.6e6, "7d": 7 * 24 * 3.6e6}[rng]
        xmin = int(df["x"].min()) if rng == "live" else int(last_ms - span)
        df = df[df["x"] >= xmin]
        fc = (m.get("rul") or {}).get("forecast") if (m.get("rul") or {}).get("sensor") == sensor and not down else None
        if down:
            xmax = max(now_ms, last_ms + 60_000) if rng != "live" else min(max(now_ms, last_ms + 30_000), last_ms + 10 * 60_000)
        else:
            xmax = int(last_ms + span * 0.35) if fc else last_ms
        fc = [f for f in (fc or []) if f["ms"] <= xmax]
        layers = []
        ys = list(df["v"])
        unit = SENSOR_UNIT[sensor]
        fmt = "%H:%M:%S" if rng == "live" else ("%H:%M" if rng == "24h" else "%a %H:%M")
        time_axis = alt.Axis(labelExpr=f"timeFormat(datum.value, '{fmt}')", labelOverlap=True, tickCount=6, labelFlush=True)
        xs = alt.X("x:Q", title=None, scale=alt.Scale(domain=[xmin, xmax], nice=False, zero=False), axis=time_axis)
        if down and m.get("down_since_ms"):
            bdf = pd.DataFrame([{"x": float(max(m["down_since_ms"], xmin)), "x2": float(xmax), "lbl": "Stopped"}])
            layers.append(alt.Chart(bdf).mark_rect(color="#ECEAE7", opacity=0.9).encode(x=xs, x2="x2:Q"))
            layers.append(alt.Chart(bdf).mark_text(align="left", dx=6, y=10, color="#B54111", fontSize=11, fontWeight=600).encode(x=xs, text="lbl:N"))
        if mdl["version"]:
            mu, sd = mdl["mean"][sensor], mdl["std"][sensor]
            band = pd.DataFrame([{"x": float(xmin), "lo": mu - 3 * sd, "hi": mu + 3 * sd}, {"x": float(xmax), "lo": mu - 3 * sd, "hi": mu + 3 * sd}])
            layers.append(alt.Chart(band).mark_area(color=K.S3, opacity=0.9).encode(x=xs, y="lo:Q", y2="hi:Q"))
            ys += [mu - 3 * sd, mu + 3 * sd]
        if fc:
            fdf = pd.DataFrame([{"x": float(f["ms"]), "v": f["v"], "lo": f["lo"], "hi": f["hi"]} for f in fc])
            layers.append(alt.Chart(fdf).mark_area(color=K.ACCENT, opacity=0.14).encode(x=xs, y="lo:Q", y2="hi:Q"))
            layers.append(alt.Chart(fdf).mark_line(color=K.ACCENT, strokeDash=[6, 4], strokeWidth=2).encode(x=xs, y="v:Q"))
            ys += list(fdf["v"])
        lo_d, hi_d = min(ys), max(ys)
        yspan = (hi_d - lo_d) or 1.0
        off = []

        def near(val):
            return lo_d - 0.6 * yspan <= val <= hi_d + 0.6 * yspan

        rules = []
        for name, val in (("warning", th["warning"]), ("critical", th["critical"])):
            if near(val):
                rules.append({"y": val, "name": f"{name} {val:g}", "dash": name})
            else:
                off.append(f'<span class="sx-pill off xs">{name.capitalize()} limit {val:g} {unit} off-scale {"↑" if val > hi_d else "↓"}</span>')
        if rules:
            rdf = pd.DataFrame(rules)
            layers.append(alt.Chart(rdf).mark_rule(color=K.ACCENT, strokeWidth=1.5).encode(
                y="y:Q", strokeDash=alt.StrokeDash("dash:N", scale=alt.Scale(domain=["warning", "critical"], range=[[5, 4], [1, 0]]), legend=None)))
            layers.append(alt.Chart(rdf).mark_text(align="left", dx=4, dy=-6, color=K.MUTED, fontSize=10).encode(
                y="y:Q", x=alt.XDatum(float(xmin)), text="name:N"))
        retrains = [e for e in snap["events"] if e.get("machine_id") == mid and e.get("kind") == "retrain" and e.get("ms", 0) >= xmin]
        if retrains:
            rt = pd.DataFrame([{"x": float(e["ms"]), "lbl": "re-learned"} for e in retrains])
            layers.append(alt.Chart(rt).mark_rule(color=K.MUTED, strokeDash=[2, 3]).encode(x=xs))
            layers.append(alt.Chart(rt).mark_text(align="left", dx=4, y=8, color=K.MUTED, fontSize=10).encode(x=xs, text="lbl:N"))
        if fc:
            now_df = pd.DataFrame([{"x": float(last_ms), "lbl": "now"}])
            layers.append(alt.Chart(now_df).mark_rule(color=K.LINE_STRONG, strokeWidth=1).encode(x=xs))
            layers.append(alt.Chart(now_df).mark_text(align="left", dx=4, y=8, color=K.SUBTLE, fontSize=10).encode(x=xs, text="lbl:N"))
        layers.append(alt.Chart(df).mark_line(color=K.FG, strokeWidth=1.4).encode(
            x=xs, y=alt.Y("v:Q", title=unit, scale=alt.Scale(zero=False, nice=True)),
            tooltip=[alt.Tooltip("x:Q", title="time", format=fmt, formatType="time"), alt.Tooltip("v:Q", title=SENSOR_LABEL[sensor], format=".3f")]))
        res = {"live": "every sample", "24h": "1-minute averages", "7d": "10-minute averages"}[rng]
        legend = (f'<div class="sx-row sx-muted" style="gap:14px;font-size:11.5px;flex-wrap:wrap;margin-bottom:2px">'
                  f'<span class="sx-row" style="gap:5px"><span style="width:14px;height:2px;background:#1B1816"></span>Measured ({res})</span>'
                  f'<span class="sx-row" style="gap:5px"><span style="width:12px;height:10px;background:#ECEAE7"></span>Normal range{" (±3σ)" if ENG else ""}</span>'
                  f'<span class="sx-row" style="gap:5px"><span style="width:14px;border-top:2px dashed #F26B1D"></span>Warning limit</span>'
                  f'<span class="sx-row" style="gap:5px"><span style="width:14px;height:2px;background:#F26B1D"></span>Critical limit</span>'
                  f'<span class="sx-row" style="gap:5px"><span style="width:12px;height:10px;background:rgba(242,107,29,.18)"></span>Forecast, likely range</span>'
                  f'{"".join(off)}</div>')
        st.html(legend)
        K.chart(alt.layer(*layers), 270)

        if rng != "live" or down:
            return
        sdf = df.dropna(subset=["score"])
        thr = mdl.get("threshold")
        lbl, _ = K.unusual(m["score"], thr)
        right = f"now <b style=\"color:#1B1816\">{K.num(m['score'], 2)}σ</b> · limit {K.num(thr, 2)}σ" if ENG else f"now <b style=\"color:#1B1816\">{lbl}</b>"
        st.html(f'<div class="sx-row" style="justify-content:space-between;margin-top:4px"><span class="sx-label">Unusual behaviour (all six signals together)</span>'
                f'<span class="sx-muted" style="font-size:12px">{right}</span></div>')
        if len(sdf):
            sx = alt.X("x:Q", title=None, scale=alt.Scale(domain=[xmin, xmax], nice=False, zero=False), axis=time_axis)
            sc = [alt.Chart(sdf).mark_area(color=K.ACCENT, opacity=0.12, line={"color": K.ACCENT, "strokeWidth": 1.4}).encode(
                x=sx, y=alt.Y("score:Q", title="σ" if ENG else None, scale=alt.Scale(zero=True), axis=alt.Axis(labels=ENG)))]
            if thr:
                sc.append(alt.Chart(pd.DataFrame([{"y": thr}])).mark_rule(color=K.ACCENT, strokeDash=[5, 4]).encode(y="y:Q"))
            K.chart(alt.layer(*sc), 110)
            if not ENG:
                st.caption("Dashed line: alert level. Above it the machine no longer behaves like its learned normal.")
        else:
            st.caption("Scoring starts when the normal pattern is learned.")


def model_card(snap: dict):
    cfg = snap["config"]
    lc = snap["loss_curve"]
    retrains = sum(1 for e in snap["events"] if e.get("kind") == "retrain")
    early = sum(1 for e in snap["events"] if e.get("kind") in ("anomaly", "drift", "degradation"))
    learning = [m for m in snap["machines"] if m["status"] != "down" and m["health"] is None]
    with K.card("online"):
        right = '<span class="sx-pill solid xs">V' + str(max([m["model"]["version"] for m in snap["machines"]] + [0])) + "</span>" if ENG else ""
        sub = (f"Learning the new normal of {', '.join(m['machine_id'] for m in learning)}" if learning
               else "Normal pattern learned for every running machine")
        st.html(K.card_title("Early-warning model", sub, "cpu", right))
        if len(lc) > 1:
            ldf = pd.DataFrame(lc)
            K.chart(alt.Chart(ldf).mark_line(color=K.ACCENT, strokeWidth=1.8).encode(
                x=alt.X("tick:Q", title="Samples learned", axis=alt.Axis(tickMinStep=1)),
                y=alt.Y("loss:Q", title="NLL" if ENG else "Error", scale=alt.Scale(zero=False), axis=alt.Axis(labels=ENG)),
                tooltip=[alt.Tooltip("tick:Q", title="sample"), alt.Tooltip("loss:Q", format=".3f")]), 130)
            st.caption("Learning curve: falling means the model is getting each machine's normal pattern right."
                       + (" Prequential negative log-likelihood (predict, then learn), averaged across machines." if ENG else ""))
        stats = [("Early warnings", str(early)), ("Process changes re-learned", str(retrains)),
                 ("Machines learned", f"{sum(1 for m in snap['machines'] if m['model']['version'])} of {len(snap['machines'])}"),
                 ("Alert level", f"{cfg['score_threshold']}σ (T²)" if ENG else (cfg.get("sensitivity") or "medium").capitalize() + " sensitivity")]
        st.html('<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 14px;margin-top:6px">' + "".join(
            f'<div><div class="sx-label">{k}</div><div class="tabular" style="font-size:13.5px;font-weight:500">{v}</div></div>' for k, v in stats) + "</div>")


def _impact(mid: str) -> dict | None:
    try:
        return impact_ops.for_machine(mid, mon.outlook(), mon.work_orders(), shell.settings())
    except Exception:
        return None  # business line is optional; telemetry must keep rendering


def next_steps_card(m: dict):
    wo = mon.store.get("work_orders", m["work_order_id"]) if m.get("work_order_id") else mon.open_work_order(m["machine_id"])
    with K.card("next", accent=True):
        st.html(K.card_title("Next steps", K.LIFECYCLE.get(m["lifecycle"], "Down"), "wrench",
                             K.pill(wo["priority"], size="xs") if wo else ""))
        if not wo:
            st.html('<div class="sx-subtle" style="font-size:12.5px">No work order yet.</div>')
            st.button("Raise work order", key="ns_raise", icon=":material/build:", on_click=maint.schedule, args=(m["machine_id"],))
            return
        st.html(f'<div class="sx-mono" style="font-size:12px;color:#57524D">{K.esc(wo["wo_id"])}</div>'
                f'<div style="font-size:13.5px;font-weight:600;margin-top:2px">{K.esc(wo["component"])}</div>'
                f'<div class="sx-muted" style="font-size:12px;margin-top:2px">{K.esc(maint.wo_status_line(wo))} · est. {wo["est_hours"]:g} h · '
                f'{K.usd(wo["est_cost_usd"])} ({K.esc(wo.get("estimate_basis") or "")})</div>'
                + maint.checklist_html(wo)
                + f'<div class="sx-muted" style="font-size:12px;margin-top:8px">{K.icon("box", 12)} {maint.parts_line(wo)}</div>'
                + maint.impact_line(_impact(m["machine_id"]))
                + (f'<div class="sx-subtle" style="font-size:12px;margin-top:3px">{K.icon("user", 12)} {K.esc(wo["technician_name"])}</div>'
                   if wo.get("technician_name") else ""))
        maint.wo_actions(wo, "ns", show_view=False)


def rul_card(m: dict):
    if m["status"] == "down":
        next_steps_card(m)
        return
    r = m.get("rul")
    d = m.get("drift") or {}
    crossed = bool(r) and ((r.get("hours") is not None and r["hours"] <= 0) or m["status"] == "critical" and (r.get("hours") or 0) <= 0)
    wo = mon.open_work_order(m["machine_id"])
    with K.card("rul", accent=bool(r)):
        act = m.get("action")
        if crossed:
            badge = K.pill("critical", "Limit crossed", "xs")
        elif act:
            badge = K.pill(act if act != "critical" else "critical", {"plan": "Plan maintenance", "urgent": "Maintenance due",
                                                                       "critical": "Act now"}[act], "xs")
        elif r:
            badge = K.pill("degradation", "Wearing", "xs")
        else:
            badge = K.pill("healthy", "Stable", "xs")
        sens = r["sensor"] if r else (d.get("sensor") or m.get("top_sensor") or SENSORS[0])
        th = SENSOR_THRESHOLDS[sens]
        st.html(K.card_title("Time to failure", f"Estimated: {SENSOR_LABEL[sens].lower()} reaching its critical limit of {th['critical']:g} {SENSOR_UNIT[sens]}",
                             "trend", badge))
        if not r:
            if m["health"] is None:
                msg = "Learning this machine's normal pattern."
            elif d.get("kind") == "classifying":
                msg = f"{SENSOR_LABEL[d['sensor']]} has changed. Checking the history to tell wear from a process change."
            else:
                msg = "No wear trend. A forecast appears as soon as a signal starts trending toward its limit."
            st.html(f'<div class="sx-subtle" style="font-size:12.5px">{K.esc(msg)}</div>')
        else:
            hrs = r.get("hours")
            if crossed:
                big = (f'<div class="sx-row" style="gap:10px;align-items:baseline"><span class="tabular" style="font-size:28px;font-weight:600;color:#B54111">Now</span>'
                       f'<span class="sx-muted" style="font-size:12px">{SENSOR_LABEL[sens]} is beyond the critical limit; the machine will stop if it stays there</span></div>')
            elif hrs is None:
                big = (f'<div class="sx-row" style="gap:10px;align-items:baseline"><span class="tabular" style="font-size:28px;font-weight:600">—</span>'
                       f'<span class="sx-muted" style="font-size:12px">The latest trend is not heading toward the limit</span></div>')
            else:
                rng_txt = f"likely {K.duration(r.get('low'))} to {K.duration(r.get('high'))}" if r.get("low") is not None and r.get("high") is not None else ""
                big = (f'<div class="tabular" style="font-size:28px;font-weight:600;color:#B54111;line-height:34px">{K.duration(hrs)}</div>'
                       f'<div class="sx-muted" style="font-size:12px">{K.esc(rng_txt)} · around {K.by_when(hrs)}</div>')
            per_h = r["slope_per_hour"]
            rate = f"{per_h:+.3g} {SENSOR_UNIT[sens]}/h" if abs(per_h) < 6 else f"{per_h / 60:+.3g} {SENSOR_UNIT[sens]}/min"
            if ENG:
                stats = [("Rate", rate), ("Fit R²", f"{r['r2']:.2f}"), ("Fitted on", r.get("window") or "recent samples"),
                         ("Trend p-value", f"{d['p_value']:.1e}" if d.get("p_value") is not None else "—")]
            else:
                stats = [("Getting worse by", rate), ("Pattern", "Repeated drift → wear" if d.get("recurring") else "Steady wear trend"),
                         ("Health now", f"{m['health']:.0f}%" if m["health"] is not None else "—"),
                         ("Advice", {"critical": "Stop and repair now", "urgent": "Schedule within a shift", "plan": "Plan maintenance"}.get(act, "Keep watching"))]
            st.html(big + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 14px;margin-top:10px">' + "".join(
                f'<div><div class="sx-label">{k}</div><div class="tabular" style="font-size:13px;font-weight:500">{K.esc(v)}</div></div>' for k, v in stats) + "</div>"
                + maint.impact_line(_impact(m["machine_id"])))
        if wo:
            st.html(f'<div class="sx-inset" style="padding:8px 10px;font-size:12px;margin-top:10px">{K.icon("wrench", 12)} '
                    f'<b>{K.esc(wo["wo_id"])}</b> {K.esc(wo["priority_label"].lower())} · {K.esc(maint.wo_status_line(wo, m))}</div>')
            maint.wo_actions(wo, "rul", show_view=False)
        elif r or m["status"] in ("warning", "critical"):
            with st.container(horizontal=True, gap="small"):
                st.button("Schedule maintenance", key="rul_sched", icon=":material/build:", type="primary",
                          on_click=maint.schedule, args=(m["machine_id"],))
                st.button("Take offline now", key="rul_off", icon=":material/power_settings_new:",
                          on_click=maint.take_offline, args=(m["machine_id"],))


def drift_table(snap: dict):
    crit = snap["config"]["psi_critical"]
    head = "".join(f'<th scope="col" style="text-align:center">{SENSOR_LABEL[s]}</th>' for s in SENSORS)
    rows = []
    for m in snap["machines"]:
        pc = m.get("psi_critical") or crit
        cells = []
        down = m["status"] == "down"
        for s in SENSORS:
            v = m["psi"].get(s, 0.0)
            a = 0 if down else (min(v / pc, 1.0) * 0.55 if pc else 0)
            hot = v >= pc and not down
            txt = f"{v:.2f}" if ENG else (K.shift(v, pc) or "·")
            cells.append(f'<td style="text-align:center;padding:3px 4px"><div class="tabular {"sx-mono" if ENG else ""}" style="background:rgba(242,107,29,{a:.2f});border-radius:6px;padding:4px 0;font-size:11.5px;'
                         f'{"outline:1.5px solid #F26B1D;font-weight:700;color:#1B1816" if hot else "color:#78716B" if down else "color:#3F3A36"}">{txt}</div></td>')
        d = m.get("drift")
        if down:
            state = K.pill(m["lifecycle"], size="xs")
        elif d and d["kind"] == "degradation":
            state = K.pill("degradation", "Wear trend", "xs")
        elif d:
            state = K.pill("drift", "Checking", "xs")
        elif m["health"] is None:
            state = '<span class="sx-subtle" style="font-size:12px">learning</span>'
        elif m["model"]["version"] > 1:
            state = f'<span class="sx-subtle" style="font-size:12px">stable · re-learned v{m["model"]["version"]}</span>'
        else:
            state = '<span class="sx-subtle" style="font-size:12px">stable</span>'
        idc = "#B54111" if m["status"] in ("critical", "down") else "#1B1816"
        rows.append(f'<tr><td class="sx-mono" style="font-weight:600;color:{idc};font-size:12px">{K.esc(m["machine_id"])}</td>{"".join(cells)}<td>{state}</td></tr>')
    sub = (f"Population Stability Index per signal vs. the learned normal · critical ≈ {crit}" if ENG
           else "How far each signal has moved from the machine's learned normal")
    with K.card("drift"):
        st.html(K.card_title("Behaviour change by signal", sub, "waves") +
                f'<div style="overflow-x:auto"><table class="sx-table"><thead><tr><th scope="col">Machine</th>{head}<th scope="col">State</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>'
                f'<div class="sx-row sx-subtle" style="gap:8px;font-size:11px;margin-top:8px">no change<span style="width:120px;height:8px;border-radius:4px;background:linear-gradient(90deg,rgba(242,107,29,0),rgba(242,107,29,.55))"></span>large change'
                f'<span style="width:12px;height:10px;border-radius:3px;outline:1.5px solid #F26B1D;margin-left:12px"></span>flagged</div>')


MAINT_KINDS = ("trip", "stopped", "repair_started", "repair_completed", "recommissioned", "work_order", "rul_plan", "rul_urgent")


def event_stream(snap: dict):
    with K.card("events"):
        c1, c2 = st.columns([2, 3], vertical_alignment="center")
        c1.html(K.card_title("Event stream", f"{len(snap['events'])} events this session", "list"))
        synced(c2.segmented_control, "Filter", ["All", "Alerts", "Maintenance", "Model"], state="event_filter", label_visibility="collapsed")
        f = ss.event_filter or "All"
        evs = list(reversed(snap["events"]))
        if f == "Alerts":
            evs = [e for e in evs if e["level"] in ("warning", "critical")]
        elif f == "Maintenance":
            evs = [e for e in evs if e.get("kind") in MAINT_KINDS]
        elif f == "Model":
            evs = [e for e in evs if e.get("machine_id") is None or e.get("kind") in ("retrain", "drift", "degradation", "recommissioned")]
        items = []
        for e in evs[:60]:
            ic = {"critical": ("alert", "#FFFFFF", "#F26B1D"), "warning": ("triangle", "#B54111", "#FFF1E8"),
                  "success": ("check", "#57524D", "#F1EFEC")}.get(e["level"], ("info", "#57524D", "#F1EFEC"))
            if e.get("kind") in MAINT_KINDS and e["level"] not in ("critical", "warning"):
                ic = ("wrench", "#57524D", "#F1EFEC")
            mid = f'<span class="sx-mono" style="font-size:11px;background:#ECEAE7;border-radius:4px;padding:0 5px;color:#1B1816">{K.esc(e["machine_id"])}</span>' if e.get("machine_id") else '<span class="sx-mono" style="font-size:11px;color:#78716B">PLANT</span>'
            detail = (f'<div class="sx-mono sx-subtle" style="font-size:10.5px;margin-top:2px">{K.esc(e["detail"])}</div>'
                      if ENG and e.get("detail") else "")
            items.append(f'<li style="list-style:none;display:grid;grid-template-columns:26px 1fr;gap:10px;padding:9px 0;border-bottom:1px solid #EFEDEA">'
                         f'<span style="width:24px;height:24px;border-radius:7px;background:{ic[2]};color:{ic[1]};display:flex;align-items:center;justify-content:center" title="{e["level"]}">{K.icon(ic[0], 14)}</span>'
                         f'<div style="min-width:0"><div class="sx-row sx-subtle" style="font-size:11px;gap:6px"><span class="tabular">{K.esc(K.ts(e["sim_time"]))}</span> {mid}</div>'
                         f'<div style="font-size:12.5px;color:#3F3A36;margin-top:2px">{K.esc(e["message"])}</div>{detail}</div></li>')
        with st.container(height=420, border=False):
            st.html(f'<ul style="margin:0;padding:0" aria-live="polite" aria-label="Monitoring events">{"".join(items) or "<li class=sx-subtle>No events yet.</li>"}</ul>')


def glossary():
    with st.popover("What do these mean?", icon=":material/help:", type="tertiary"):
        st.html("".join(f'<div style="margin-bottom:10px"><div style="font-weight:600;font-size:13px">{K.esc(t)}</div>'
                        f'<div class="sx-muted" style="font-size:12.5px">{K.esc(d)}</div></div>' for t, d in K.GLOSSARY))
        st.caption("Turn on Engineer view in Settings → Appearance to see the statistics behind each label.")


@st.fragment(run_every=shell.settings()["display"]["refresh_s"])
def live_view():
    shell.follow_nav()
    maint.fragment_hook()
    chat_dock.fragment_hook()
    mon.touch()
    if not mon.active:
        ss.pop("live_frozen", None)
        st.rerun()
    snap = ss.get("live_frozen") or mon.snapshot()
    session_strip(snap)
    alert_banner(snap)
    focus = _pick_focus(snap["machines"])

    c1, c2, c3 = st.columns([4, 2, 2], vertical_alignment="bottom")
    c1.html('<div class="sx-label" style="margin-top:4px">Fleet · health and unusual behaviour</div>')
    with c2:
        glossary()
    synced(c3.toggle, "Follow highest risk", state="follow_risk")
    fleet_grid(snap, focus)
    st.pills("Inspect machine", [m["machine_id"] for m in snap["machines"]], key="pick_machine", on_change=_on_pick,
             label_visibility="collapsed")

    left, right = st.columns([5, 3])
    with left:
        focus_panel(snap, focus)
    with right:
        rul_card(focus)
        model_card(snap)
    left, right = st.columns([7, 5])
    with left:
        drift_table(snap)
    with right:
        event_stream(snap)


maint.page_dialog()
if mon.active:
    mon.touch()
    live_view()
else:
    setup_view()
