"""
Plant Assistant chat dock: a launcher in the bottom-right corner of every page
that opens a chat panel backed by the PLANT_ASSISTANT Cortex Agent.

The dock is one fragment mounted after the page. A question runs on a worker
thread (services/assistant.py); while it runs the fragment polls once a second,
so the rest of the page keeps updating and the user can keep working.
"""

import time

import streamlit as st

from snowflake_app.services import assistant as A
from snowflake_app.ui import kit as K, shell

ss = st.session_state
SAMPLES = ["Which machines are down right now and why?", "Which machines need maintenance in the next 3 days?",
           "How many failures did we catch early versus breakdowns?", "What did unscheduled maintenance cost by line?"]


def live_context() -> str:
    """Short real-time summary sent with each question (live state can be newer than the tables)."""
    mon = shell.monitor()
    lines = []
    if mon.engine.session_id and mon.active:
        snap = mon.snapshot()
        ms = snap["machines"]
        lines.append(f"Live monitoring session {snap['session_id']}, last sample {snap['sim_time']}. "
                     f"{sum(1 for m in ms if m['status'] != 'down')} of {len(ms)} machines running.")
        down = [m for m in ms if m["status"] == "down"]
        if down:
            lines.append("Down: " + "; ".join(f"{m['machine_id']} {m['name']} ({K.LIFECYCLE.get(m['lifecycle'], 'down').lower()}"
                                              f"{', work order ' + m['work_order_id'] if m.get('work_order_id') else ''})" for m in down) + ".")
        risk = sorted([m for m in ms if (m.get("rul") or {}).get("hours") is not None and m["status"] != "down"],
                      key=lambda m: m["rul"]["hours"])[:4]
        if risk:
            lines.append("Time to failure: " + "; ".join(
                f"{m['machine_id']} {m['name']} about {K.duration(m['rul']['hours'])} ({K.sensor_label(m['rul']['sensor']).lower()}, "
                f"health {m['health']:.0f}%)" for m in risk) + ".")
    else:
        lines.append("Live monitoring is not running; use the stored machine state and history.")
    k = mon.plant_kpis()
    if k["open"]:
        lines.append(f"Open work orders: {', '.join(w['wo_id'] + ' ' + w['machine_id'] + ' ' + w['status'] for w in k['open'][:6])}.")
    focus = ss.get("chat_machine") or ss.get("focus_machine")
    if focus:
        lines.append(f"The user is looking at {focus}.")
    return "\n".join(lines)


def ask(question: str, machine: str | None = None):
    """Start a turn (usable as an on_click callback from any page)."""
    question = (question or "").strip()
    if not question or ss.get("chat_job"):
        return
    if machine:
        ss.chat_machine = machine
    ss.chat_open = True
    hist = list(ss.get("chat_msgs") or [])
    ss.setdefault("chat_msgs", []).append({"role": "user", "text": question})
    ss.chat_job = A.submit(shell.settings(), hist, question, live_context())
    ss._chat_kick = True


def ask_about(machine_id: str):
    ask(f"What is going on with {machine_id}, and what should we do next?", machine=machine_id)


def fragment_hook():
    """Call inside other refreshing fragments: a turn started there needs one full rerun to show the dock."""
    if ss.pop("_chat_kick", False):
        st.rerun(scope="app")


def _open():
    ss.chat_open = True
    ss._chat_kick = True


def _close():
    ss.chat_open = False
    ss._chat_kick = True


def _clear():
    ss.chat_msgs = []
    ss.pop("chat_job", None)
    ss.pop("chat_machine", None)


def _submit():
    ask(ss.get("chat_in") or "")


def _cell(v):
    """Result-set values arrive as strings; show numbers rounded."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return f"{f:,.0f}" if f.is_integer() else f"{f:,.2f}"


def _render(m: dict, i: int):
    with st.chat_message(m["role"], avatar=":material/person:" if m["role"] == "user" else ":material/smart_toy:"):
        if m.get("error"):
            st.error(m["error"], icon=":material/error:")
            return
        st.markdown(m.get("text") or "")
        for t in m.get("tables") or []:
            if t["rows"]:
                cols = [(c, c.replace("_", " ").title()) for c in (t["columns"] or [f"c{k}" for k in range(len(t["rows"][0]))])]
                rows = [dict(zip([c for c, _ in cols], [_cell(v) for v in r])) for r in t["rows"][:12]]
                more = f'<div class="sx-subtle" style="font-size:11px">{len(t["rows"]) - 12} more rows</div>' if len(t["rows"]) > 12 else ""
                st.html(f'<div style="font-size:12px">{K.table(cols, rows)}</div>{more}')
        if m.get("sql") and K.engineer():
            with st.expander("SQL", icon=":material/code:"):
                for q in m["sql"]:
                    st.code(q, language="sql")
        if m.get("latency_s") is not None:
            st.caption(f"{m['latency_s']} s · Cortex Agent")


def mount():
    if not (shell.settings().get("assistant") or {}).get("enabled", True):
        return
    ss.setdefault("chat_open", False)
    ss.setdefault("chat_msgs", [])
    polling = bool(ss.get("chat_open") and ss.get("chat_job"))

    @st.fragment(run_every=1.0 if polling else None)
    def dock():
        if ss.pop("_chat_kick", False):
            st.rerun(scope="app")
        job = ss.get("chat_job")
        if job:
            j = A.poll(job)
            if not j or j["status"] != "running":
                r = (j or {}).get("result") or {}
                ss.chat_msgs.append({"role": "assistant", "text": r.get("text"), "tables": r.get("tables"), "sql": r.get("sql"),
                                     "suggestions": r.get("suggestions"), "latency_s": r.get("latency_s"),
                                     "error": (j or {}).get("error") if (not j or j["status"] == "failed") else None})
                ss.pop("chat_job", None)
                st.rerun(scope="app")  # stop polling
        with st.container(key="chatdock"):
            if not ss.chat_open:
                st.button("Ask Plant Assistant", icon=":material/forum:", key="chat_launch", type="primary", on_click=_open)
                return
            with st.container(key="chatpanel"):
                with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                    st.html(f'<div class="sx-row" style="gap:10px"><span style="width:30px;height:30px;border-radius:9px;background:#F26B1D;'
                            f'color:#fff;display:flex;align-items:center;justify-content:center">{K.icon("cpu", 16)}</span>'
                            f'<div><div style="font-weight:600;font-size:14px;line-height:18px">Plant Assistant</div>'
                            f'<div class="sx-subtle" style="font-size:11px">Cortex Agent · live plant data</div></div></div>')
                    st.space("stretch")
                    if ss.chat_msgs:
                        st.button("", icon=":material/delete_sweep:", key="chat_clear", type="tertiary", on_click=_clear,
                                  help="Clear the conversation")
                    st.button("", icon=":material/close:", key="chat_close", type="tertiary", on_click=_close, help="Close")
                with st.container(height=380, border=False, key="chatmsgs"):
                    if not ss.chat_msgs:
                        st.html('<div class="sx-muted" style="font-size:12.5px;margin:4px 0 8px">Ask about machines, downtime, work orders, '
                                'maintenance cost or defects. Answers come from your Snowflake data and the live monitor.</div>')
                        for q in SAMPLES:
                            st.button(q, key=f"chat_s_{q[:18]}", type="secondary", on_click=ask, args=(q,), width="stretch")
                    for i, m in enumerate(ss.chat_msgs):
                        _render(m, i)
                    if job:
                        started = (A.poll(job) or {}).get("started") or time.time()
                        with st.chat_message("assistant", avatar=":material/smart_toy:"):
                            st.html(f'<div class="sx-row sx-muted" style="gap:8px;font-size:12.5px">{K.icon("loader", 14, "sx-spin")}'
                                    f'Looking at the plant data… {time.time() - started:.0f}s</div>')
                    last = ss.chat_msgs[-1] if ss.chat_msgs else None
                    if last and last["role"] == "assistant" and last.get("suggestions") and not job:
                        for i, q in enumerate(last["suggestions"][:3]):
                            st.button(q, key=f"chat_f_{len(ss.chat_msgs)}_{i}", type="tertiary", on_click=ask, args=(q,),
                                      icon=":material/subdirectory_arrow_right:")
                st.chat_input("Ask about machines, work orders, costs…", key="chat_in", on_submit=_submit,
                              disabled=bool(job))

    dock()
