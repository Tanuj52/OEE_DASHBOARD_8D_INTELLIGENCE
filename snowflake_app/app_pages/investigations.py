import time
from datetime import datetime

import streamlit as st

from snowflake_app.core.pipeline import STEPS, trigger_options
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/investigations.py"
shell.topbar(PATH)

rm = shell.run_manager()
ss = st.session_state
ss.setdefault("tl_expand", False)
STATE = {"done": "complete", "failed": "error", "running": "running", "thinking": "running"}


def _selected() -> dict | None:
    if not ss.selected_run or not rm.get(ss.selected_run):
        runs = rm.list()
        ss.selected_run = runs[0]["id"] if runs else None
    return rm.get(ss.selected_run) if ss.selected_run else None


def _tool(snap: dict, step: str, tool: str):
    for s in snap["steps"]:
        if s["id"] == step:
            for t in s.get("tools", []):
                if t["tool"] == tool and t.get("result"):
                    return t["result"]
    return {}


def _elapsed(snap: dict) -> str:
    start = datetime.fromisoformat(snap["created_at"])
    end = datetime.fromisoformat(snap["finished_at"]) if snap.get("finished_at") else datetime.now()
    s = max(int((end - start).total_seconds()), 0)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


# ------------------------------------------------------------------ left column
def new_investigation():
    mon = shell.monitor()
    live_alerts = mon.snapshot()["alerts"] if mon.active else []
    opts = trigger_options(live_alerts)
    prov = shell.active_provider()
    busy = rm.active() is not None
    with K.card("new"):
        st.html(K.card_title("New investigation", "What should the agents investigate?", "branch"))
        idx = st.radio("Trigger", range(len(opts)), key="trigger_idx", label_visibility="collapsed",
                       format_func=lambda i: opts[i]["label"],
                       captions=[f"{o['detail']} · risk {o['risk']}" for o in opts])
        ico = "database" if prov["provider"] == "snowflake" else "cpu"
        st.html(f'<div class="sx-inset sx-row" style="padding:10px 12px;gap:10px;font-size:12.5px;margin-top:4px">'
                f'<span class="sx-subtle">{K.icon(ico, 16)}</span><span class="sx-muted">Agents run on</span>'
                f'<b style="font-weight:600">{K.esc(prov["label"])}</b><span class="sx-mono sx-subtle" style="margin-left:auto">{K.esc(prov["model"])}</span></div>')
        if not prov["configured"]:
            st.warning(f"{prov['label']} is not configured yet. Add credentials in Settings first.", icon=":material/key:")
            st.page_link("app_pages/settings.py", label="Open Settings", icon=":material/settings:")
        st.button("Investigation running…" if busy else "Run 8D investigation", type="primary", icon=":material/play_arrow:",
                  key="run_btn", disabled=busy or not prov["configured"] or idx is None, width="stretch",
                  on_click=_run_selected, args=(opts,))
        if ss.get("run_error"):
            st.error(ss.pop("run_error"), icon=":material/error:")


def _run_selected(opts: list[dict]):
    o = opts[ss.trigger_idx]
    _, err = shell.start_investigation(o["source"], o["machine_id"])
    if err:
        ss.run_error = err


def _cancel(run_id: str):
    rm.cancel(run_id)
    shell.notify("Cancelling after the current agent finishes…", icon=":material/stop:")


def run_summary(snap: dict):
    t = snap["trigger"]
    p = snap["progress"]
    running = snap["status"] in ("queued", "running")
    with K.card("summary"):
        st.html(K.card_title(snap["id"], f"Started {datetime.fromisoformat(snap['created_at']):%b %-d, %Y · %I:%M %p}", "list", K.pill(snap["status"], size="xs")))
        st.html(f'<div class="sx-row" style="justify-content:space-between;font-size:12px;margin-bottom:6px"><span class="sx-muted">{p["done"]} of {p["total"]} agents</span>'
                f'<span class="tabular">{_elapsed(snap)}</span></div>{K.bar(p["done"] / p["total"] * 100, True, running, "Pipeline progress")}')
        sig = f'{K.sensor_label(t.get("sensor"))} {K.sensor_val(t.get("sensor"), t.get("value"))} {K.sensor_unit(t.get("sensor"))}' if t.get("sensor") else "Defect cluster"
        st.html(f'<div class="sx-inset" style="padding:10px 12px;margin-top:12px"><div class="sx-label">Trigger · {K.esc(t.get("source"))}</div>'
                f'<div style="font-weight:600;margin-top:2px">{K.esc(t.get("machine_id"))} {K.esc(t.get("machine_name"))} <span class="sx-muted" style="font-weight:400">· {K.esc(t.get("line"))}</span></div>'
                f'<div class="sx-muted" style="font-size:12px">{K.esc(sig)} · risk {K.esc(t.get("risk") or "—")}</div></div>')
        if snap.get("decision"):
            d = snap["decision"]
            st.html(f'<div class="sx-row" style="gap:8px;margin-top:10px;padding:8px 12px;border-radius:9px;border:1px solid rgba(242,107,29,.45);background:#FFF1E8;font-size:12.5px">'
                    f'<span class="sx-accent">{K.icon("diamond", 14)}</span><span><b>Routing decision:</b> {K.esc(d["message"])}</span></div>')
        if snap.get("error"):
            st.error(snap["error"], icon=":material/error:")
        bd = _tool(snap, "d0", "defect_breakdown")
        spc = _tool(snap, "d4", "spc")
        tr = (_tool(snap, "d3", "trace").get("impact") or {})
        copq = _tool(snap, "d8", "copq")
        stats = [("Fail rate on machine", f"{bd['machine_fail_rate_pct']}% vs {bd['rest_of_plant_fail_rate_pct']}%" if bd else "—"),
                 ("Cpk", f"{spc['cpk']} ({spc['characteristic']})" if spc else "—"),
                 ("Units at risk", K.num(tr.get("total_units_at_risk")) if tr else "—"),
                 ("Customers affected", K.num(tr.get("total_customers_affected")) if tr else "—"),
                 ("Cost of poor quality", K.usd(copq.get("total_copq")) if copq else "—"),
                 ("Tokens", K.num(snap.get("total_tokens")))]
        st.html('<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 14px;margin-top:12px">' + "".join(
            f'<div><div class="sx-label">{k}</div><div class="tabular" style="font-size:13.5px;font-weight:500">{K.esc(v)}</div></div>' for k, v in stats) + "</div>")
        with st.container(horizontal=True):
            if running:
                st.button("Cancel run", icon=":material/stop:", key="cancel_btn", on_click=_cancel, args=(snap["id"],))
            if snap["status"] == "completed":
                st.button("Open report", icon=":material/description:", key="open_report", on_click=_open_report, args=(snap["id"],))


def _open_report(run_id: str):
    shell.open_report(run_id)


def _select(run_id: str):
    ss.selected_run = run_id


def history():
    runs = rm.list()
    queue = rm.queued()
    if queue:
        with K.card("queue"):
            st.html(K.card_title("Queued", f"{len(queue)} waiting · started automatically after a machine stopped", "clock"))
            for q in queue:
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                c1.html(f'<div style="font-size:12.5px"><span class="sx-mono">{K.esc(q["machine_id"] or "")}</span> '
                        f'<span class="sx-muted">{K.esc(q["label"])} · since {K.esc(K.ts(q["queued_at"]))}</span></div>')
                c2.button("", icon=":material/close:", key=f"q_x_{q['id']}", type="tertiary", help="Remove from the queue",
                          on_click=rm.cancel_queued, args=(q["id"],))
    if not runs:
        return
    with K.card("history"):
        st.html(K.card_title("History", f"{len(runs)} investigation{'s' if len(runs) != 1 else ''}", "clock"))
        for r in runs[:10]:
            t = r["trigger"]
            on = r["id"] == ss.selected_run
            badge = {"completed": ":gray-badge[completed]", "running": ":orange-badge[running]", "failed": ":red-badge[failed]",
                     "cancelled": ":gray-badge[cancelled]", "queued": ":gray-badge[queued]"}.get(r["status"], "")
            label = f"{'**' if on else ''}`{r['id']}`{'**' if on else ''} · {t.get('machine_id')} {badge}"
            st.button(label, key=f"hist_{r['id']}", type="tertiary", on_click=_select, args=(r["id"],))


# ----------------------------------------------------------------- right column
def state_machine(snap: dict | None):
    steps = {s["id"]: s for s in (snap or {}).get("steps", [])}
    dec = (snap or {}).get("decision")
    chips = []
    for s in STEPS:
        st_ = steps.get(s["id"], {}).get("status", "pending")
        ico = {"done": "check", "running": "loader", "thinking": "loader", "failed": "xcircle"}.get(st_, "minus")
        hot = st_ in ("running", "thinking")
        style = ("background:#F26B1D;border-color:#F26B1D;color:#FFFFFF" if hot else
                 "background:#fff;border-color:#D0CCC7;color:#1B1816" if st_ == "done" else
                 "background:#FDECE3;border-color:#C2410C;color:#9A3412" if st_ == "failed" else
                 "background:#F5F4F2;border-color:#E5E2DE;color:#A8A29E")
        chips.append(f'<span class="sx-row sx-mono" style="gap:5px;border:1px solid;border-radius:8px;padding:4px 9px;font-size:11.5px;font-weight:600;{style}" '
                     f'title="{K.esc(s["title"])}: {st_}">{K.icon(ico, 12)}{K.esc(s["code"])}</span>')
        if s["id"] == "predictive":
            txt = "YES" if dec and dec["escalate"] else ("NO" if dec else "?")
            chips.append(f'<span title="Conditional edge: escalate to 8D?" style="width:30px;height:30px;transform:rotate(45deg);border:1.5px solid #F26B1D;'
                         f'background:{"#FFF1E8" if dec else "#fff"};display:inline-flex;align-items:center;justify-content:center;margin:0 4px">'
                         f'<span style="transform:rotate(-45deg);font-size:9px;font-weight:700;color:#B54111">{txt}</span></span>')
        if s["id"] != "d8":
            chips.append('<span style="width:10px;height:1px;background:#D0CCC7"></span>')
    with K.card("graph"):
        st.html(f'<div class="sx-row" style="justify-content:space-between;flex-wrap:wrap;margin-bottom:8px"><span class="sx-label">Agent state machine</span>'
                f'<span class="sx-subtle" style="font-size:11.5px">Conditional edge after PdM: escalate only if risk warrants a full 8D</span></div>'
                f'<div class="sx-row" style="flex-wrap:wrap;gap:4px" role="img" aria-label="Orchestration graph">{"".join(chips)}</div>')


def timeline(snap: dict):
    running = snap["status"] in ("queued", "running")
    last_done = next((s["id"] for s in reversed(snap["steps"]) if s["status"] == "done"), None)
    with K.card("timeline"):
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.html(K.card_title("Agent timeline", "Tools gather evidence from plant data, then the agent reasons over it", "list"))
        shell.synced(c2.toggle, "Expand all", state="tl_expand")
        for s in snap["steps"]:
            tools = s.get("tools", [])
            tok = (s.get("usage") or {}).get("total_tokens")
            meta = " · ".join(x for x in [f"{len(tools)} tool{'s' if len(tools) != 1 else ''}" if tools else "",
                                          f"{tok:,} tok" if tok else "", f"{s['latency_s']}s" if s.get("latency_s") else ""] if x)
            label = f"**{s['code']}** · {s['title']} — {s['subtitle']}" + (f"  :gray[{meta}]" if meta else "")
            status = s["status"]
            expanded = ss.tl_expand or status in ("running", "thinking", "failed") or (not running and s["id"] == last_done)
            if status in STATE:
                if status == "thinking":
                    waited = time.time() - (s.get("llm_started_at") or time.time())
                    label = f"**{s['code']}** · {s['title']} — :shimmer[reasoning with {snap['provider'].get('model')} · {waited:.0f}s]"
                box = st.status(label, type="step", state=STATE[status], expanded=expanded)
            else:
                box = st.expander(label + ("  :gray[skipped]" if status == "skipped" else "  :gray[pending]"), type="step",
                                  icon=":material/radio_button_unchecked:", expanded=False)
            with box:
                step_body(snap, s)


def step_body(snap: dict, s: dict):
    if s["status"] in ("pending", "skipped"):
        st.caption("Skipped: the conditional edge ended the pipeline after PdM." if s["status"] == "skipped" else "Waiting for upstream agents.")
        return
    rows = []
    for t in s.get("tools", []):
        dur = f"{t['duration_s'] * 1000:.0f} ms" if t.get("duration_s") is not None else "running…"
        summ = f'<div class="sx-muted" style="font-size:12px;margin-top:2px"><span class="sx-accent">→</span> {K.esc(t.get("summary"))}</div>' if t.get("summary") else ""
        rows.append(f'<div class="sx-inset" style="padding:8px 11px"><div class="sx-row" style="gap:8px;font-size:12.5px">'
                    f'<span class="sx-subtle">{K.icon("wrench", 13)}</span><span class="sx-mono" style="font-weight:600">{K.esc(t["tool"])}()</span>'
                    f'<span class="sx-muted">{K.esc(t["label"])}</span><span class="sx-subtle tabular" style="margin-left:auto">{dur}</span></div>{summ}</div>')
    if rows:
        st.html(f'<div style="display:grid;gap:6px">{"".join(rows)}</div>')
    if s.get("output"):
        with st.container(key=f"agentout_{s['id']}"):
            st.markdown(K.md(s["output"]))
    elif s["status"] in ("running", "thinking"):
        st.caption(":shimmer[Agent is reasoning over the evidence…]")
    if s["status"] == "failed":
        st.error(s.get("error") or "Agent failed", icon=":material/error:")
    if s["id"] == "predictive" and snap.get("decision"):
        d = snap["decision"]
        st.html(f'<div class="sx-row" style="gap:8px;padding:8px 12px;border-radius:9px;border:1px dashed rgba(242,107,29,.7);font-size:12.5px">'
                f'<span class="sx-accent">{K.icon("diamond", 14)}</span><b>Conditional edge:</b> {K.esc(d["message"])}</div>')


# ----------------------------------------------------------------------- page
def body():
    snap = _selected()
    left, right = st.columns([5, 8])
    with left:
        new_investigation()
        if snap:
            run_summary(snap)
        history()
    with right:
        state_machine(snap)
        if snap:
            timeline(snap)
        else:
            with K.card("empty"):
                st.html('<div style="text-align:center;padding:40px 10px"><div style="font-weight:600">No investigation selected</div>'
                        '<div class="sx-subtle" style="font-size:12.5px;margin-top:4px">Pick a trigger and run the 11-agent pipeline. '
                        'Each agent calls plant-data tools, then reasons with the selected model.</div></div>')
    return snap


@st.fragment(run_every=1)
def live_body():
    shell.follow_nav()
    snap = body()
    if not snap or snap["status"] not in ("queued", "running"):
        st.rerun()  # finished: drop back to the static page


sel = _selected()
if sel and sel["status"] in ("queued", "running"):
    live_body()
else:
    body()
