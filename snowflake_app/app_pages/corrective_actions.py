import time
from datetime import datetime, time as dtime

import streamlit as st

from snowflake_app.core import capa as C
from snowflake_app.services import capa_ops
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/corrective_actions.py"
shell.topbar(PATH)
ss = st.session_state
mon = shell.monitor()
rm = shell.run_manager()
NOW = time.time()
rm.import_legacy()
capa_ops.backfill()
capa_ops.check_effectiveness(mon.work_orders(), NOW)
ACTIONS = capa_ops.actions()
OPS = capa_ops.operators()
OP_NAME = {o["operator_id"]: o["name"] for o in OPS}
for k, v in (("ca_view", "due"), ("ca_status", "open"), ("ca_owner", "all"), ("ca_machine", "all"), ("ca_q", "")):
    ss.setdefault(k, v)
DISC_PILL = {"D3": ("warn", "Containment"), "D6": ("solid", "Corrective"), "D7": ("off", "Preventive")}


# ------------------------------------------------------------------ callbacks
def _status(aid: str, status: str, note: str = ""):
    a = capa_ops.set_status(aid, status, shell.current_user(), note)
    if a:
        shell.notify(f"{C.STATUS_LABEL[status]}: {a['action'][:60]}", icon=":material/task_alt:")


def _reassign(aid: str):
    new = ss.get(f"own_{aid}")
    if capa_ops.reassign(aid, new, shell.current_user()):
        shell.notify(f"Reassigned to {OP_NAME.get(new, new)}", icon=":material/person:")


def _note(aid: str):
    text = ss.get(f"note_{aid}", "")
    if capa_ops.add_note(aid, text, shell.current_user()):
        ss[f"note_{aid}"] = ""
        shell.notify("Note added", icon=":material/sticky_note_2:")


def _due(aid: str):
    d = ss.get(f"due_{aid}")
    if d:
        iso = datetime.combine(d, dtime(17, 0)).astimezone().isoformat(timespec="seconds")
        capa_ops.set_due(aid, iso, shell.current_user())
        shell.notify(f"Due date moved to {d:%b %-d}", icon=":material/event:")


def _new_8d(mid: str):
    shell.launch_investigation("machine", mid)


# ------------------------------------------------------------------ header
k = capa_ops.kpis(NOW)
tiles = [
    K.kpi("Open actions", str(k["open"]), "", f"{k['total']} tracked from {len({a['run_id'] for a in ACTIONS})} reports", ico="list"),
    K.kpi("Overdue", str(k["overdue"]), "", "Past their due date", ico="clock", emphasis=k["overdue"] > 0),
    K.kpi("Due this week", str(k["due_week"]), "", "Next 7 days", ico="trend"),
    K.kpi("Fixes that worked", f"{k['verified_pct']}" if k["verified_pct"] is not None else "—", "%" if k["verified_pct"] is not None else "",
          f"{k['awaiting_verification']} waiting for the {C.VERIFY_DAYS}-day check" if k["awaiting_verification"] else "Verified effective",
          ico="shield", emphasis=bool(k["not_effective"])),
    K.kpi("Time to close", f"{k['median_days_to_close']:g}" if k["median_days_to_close"] is not None else "—",
          "days" if k["median_days_to_close"] is not None else "", "Median, created to done", ico="gauge"),
]
st.html(f'<section class="sx-kpis" aria-label="Corrective action indicators">{"".join(tiles)}</section>')

if not ACTIONS:
    with K.card("ca_empty"):
        st.html('<div style="text-align:center;padding:30px 10px"><div style="font-weight:600;font-size:15px">No actions yet</div>'
                '<div class="sx-subtle" style="font-size:12.5px;margin-top:4px">Every finished 8D investigation adds its containment, '
                'corrective and preventive actions here with a named owner and a due date.</div></div>')
        with st.container(horizontal=True, horizontal_alignment="center"):
            st.button("Open 8D Investigations", icon=":material/account_tree:", type="primary",
                      on_click=shell.goto, args=("app_pages/investigations.py",))
    st.stop()

# ------------------------------------------------------------------ filters
with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
    shell.synced(st.segmented_control, "View", ["due", "report"], state="ca_view",
                 format_func=lambda v: ":material/event: By due date" if v == "due" else ":material/description: By report")
    shell.synced(st.segmented_control, "Show", ["open", "verify", "closed", "all"], state="ca_status",
                 format_func={"open": "Open", "verify": "To verify", "closed": "Closed", "all": "All"}.get)
    owners = ["all"] + sorted({a["owner_id"] for a in ACTIONS if a.get("owner_id")}, key=lambda i: OP_NAME.get(i, i))
    shell.synced(st.selectbox, "Owner", owners, state="ca_owner", format_func=lambda i: "Everyone" if i == "all" else OP_NAME.get(i, i),
                 width=200)
    machines = ["all"] + sorted({a["machine_id"] for a in ACTIONS if a.get("machine_id")})
    shell.synced(st.selectbox, "Machine", machines, state="ca_machine", format_func=lambda m: "All machines" if m == "all" else m,
                 width=160)
    shell.synced(st.text_input, "Search", state="ca_q", placeholder="Action, report or owner", width=240)


def visible(a: dict) -> bool:
    s = a["status"]
    keep = {"open": s in C.OPEN, "verify": s in ("done", "not_effective"), "closed": s in ("verified", "cancelled") or
            (s == "done" and a["discipline"] != "D6"), "all": True}[ss.ca_status]
    if ss.ca_status == "verify" and s == "done" and a["discipline"] != "D6":
        keep = False
    q = ss.ca_q.strip().lower()
    return (keep and (ss.ca_owner == "all" or a.get("owner_id") == ss.ca_owner)
            and (ss.ca_machine == "all" or a.get("machine_id") == ss.ca_machine)
            and (not q or q in " ".join(str(a.get(x) or "") for x in ("action", "run_id", "owner_name", "machine_id", "owner_role")).lower()))


rows = [a for a in ACTIONS if visible(a)]


def due_html(a: dict) -> str:
    due = C._epoch(a.get("due_at"))
    if due is None:
        return "No due date"
    if a["status"] in C.OPEN and due < NOW:
        return f'<b style="color:#B54111">Overdue {K.duration((NOW - due) / 3600)}</b> · was due {K.esc(K.ts(a["due_at"]))}'
    if a["status"] in C.OPEN:
        return f'Due {K.esc(K.by_when((due - NOW) / 3600))}'
    when = a.get("verified_at") or a.get("done_at")
    return f'{K.esc(C.STATUS_LABEL[a["status"]])} {K.esc(K.ts(when)) if when else ""}'


def action_card(a: dict, key: str):
    aid = a["action_id"]
    v, label = DISC_PILL.get(a["discipline"], ("off", a["discipline"]))
    hot = C.is_overdue(a, NOW) or a["status"] == "not_effective"
    status_pill = {"open": K.pill("pending", "Open", "xs"), "in_progress": K.pill("running", "In progress", "xs"),
                   "done": K.pill("done", "Done", "xs"), "verified": K.pill("completed", "Verified effective", "xs"),
                   "not_effective": K.pill("failed", "Not effective", "xs"), "cancelled": K.pill("cancelled", "Cancelled", "xs")}[a["status"]]
    extra = ""
    if a.get("addresses"):
        extra += f'<span class="sx-subtle"> · addresses {K.esc(a["addresses"])}</span>'
    if a.get("result"):
        extra += f'<div class="sx-muted" style="font-size:12px;margin-top:4px">{K.icon("shield", 12)} {K.esc(a["result"])}</div>'
    with st.container(border=True, key=f"card_ca_{key}_{aid}"):
        c1, c2 = st.columns([7, 3], vertical_alignment="top", gap="medium")
        c1.html(f'<div class="sx-row" style="gap:6px;flex-wrap:wrap"><span class="sx-pill {v} xs">{K.esc(a["discipline"])} · {K.esc(label)}</span>'
                f'{status_pill}<span class="sx-mono sx-subtle" style="font-size:11px">{K.esc(a.get("machine_id") or "")} · {K.esc(a["run_id"])}</span></div>'
                f'<div style="font-size:14px;font-weight:600;line-height:20px;margin-top:6px{";color:#B54111" if hot else ""}">{K.esc(a["action"])}</div>'
                f'<div class="sx-muted" style="font-size:12.5px;margin-top:3px">{due_html(a)}{extra}</div>')
        with c2:
            ids = [o["operator_id"] for o in OPS]
            if a.get("owner_id") in ids:
                ss.setdefault(f"own_{aid}", a["owner_id"])
                st.selectbox("Owner", ids, key=f"own_{aid}", format_func=lambda i: OP_NAME.get(i, i), on_change=_reassign, args=(aid,),
                             help=f"Role in the report: {a.get('owner_role') or '—'}", disabled=a["status"] in ("verified", "cancelled"))
            else:
                st.html(f'<div class="sx-muted" style="font-size:12.5px">{K.icon("user", 12)} {K.esc(a.get("owner_name") or "Unassigned")}</div>')
        with st.container(horizontal=True, gap="small"):
            s = a["status"]
            if s == "open":
                st.button("Start", key=f"{key}_st_{aid}", icon=":material/play_arrow:", on_click=_status, args=(aid, "in_progress"))
            if s in C.OPEN:
                st.button("Mark done", key=f"{key}_dn_{aid}", icon=":material/check:", type="primary", on_click=_status, args=(aid, "done"))
            if s == "done" and a["discipline"] == "D6":
                st.button("Verify effective", key=f"{key}_vf_{aid}", icon=":material/verified:", type="primary",
                          on_click=_status, args=(aid, "verified", "Verified by the owner"),
                          help=f"The app also verifies it automatically when {a.get('machine_id')} has no breakdown for {C.VERIFY_DAYS} days")
                st.button("Not effective", key=f"{key}_ne_{aid}", icon=":material/report:", on_click=_status,
                          args=(aid, "not_effective", "The problem came back"))
            if s == "not_effective":
                st.button("Start a new 8D", key=f"{key}_8d_{aid}", icon=":material/account_tree:", type="primary",
                          on_click=_new_8d, args=(a["machine_id"],), disabled=not a.get("machine_id"))
                st.button("Rework", key=f"{key}_rw_{aid}", icon=":material/replay:", on_click=_status, args=(aid, "in_progress", "Reworking the fix"))
            if s in C.OPEN:
                st.button("Cancel", key=f"{key}_cx_{aid}", type="tertiary", on_click=_status, args=(aid, "cancelled", "Not needed"))
            if s in ("verified", "cancelled", "done") and not (s == "done" and a["discipline"] == "D6"):
                st.button("Reopen", key=f"{key}_ro_{aid}", type="tertiary", icon=":material/undo:",
                          on_click=_status, args=(aid, "in_progress" if s != "cancelled" else "open"))
            n = len(a.get("notes") or [])
            with st.popover(f"Notes · {n}" if n else "Details", icon=":material/sticky_note_2:"):
                for note in a.get("notes") or []:
                    st.html(f'<div style="font-size:12.5px"><b>{K.esc(note["by"])}</b> <span class="sx-subtle">{K.esc(K.ts(note["at"]))}</span>'
                            f'<div>{K.esc(note["text"])}</div></div>')
                st.text_area("Add a note", key=f"note_{aid}", height=70, placeholder="Progress, evidence, blockers")
                st.button("Add note", key=f"{key}_nt_{aid}", icon=":material/add:", on_click=_note, args=(aid,))
                if s in C.OPEN:
                    due = C._epoch(a.get("due_at"))
                    ss.setdefault(f"due_{aid}", datetime.fromtimestamp(due).date() if due else datetime.now().date())
                    st.date_input("Due date", key=f"due_{aid}", on_change=_due, args=(aid,))
                st.html('<div class="sx-label" style="margin-top:6px">History</div>' + "".join(
                    f'<div class="sx-subtle" style="font-size:11.5px">{K.esc(K.ts(h["at"]))} · {K.esc(h["by"])}: {K.esc(h["what"])}</div>'
                    for h in reversed(a.get("history") or [])))
            st.button("Report", key=f"{key}_rp_{aid}", type="tertiary", icon=":material/article:", on_click=shell.open_report, args=(a["run_id"],))


if not rows:
    with K.card("ca_none"):
        st.html('<div class="sx-subtle" style="text-align:center;padding:18px;font-size:13px">No actions match these filters.</div>')
elif ss.ca_view == "due":
    groups = [("Overdue", [a for a in rows if C.is_overdue(a, NOW)]),
              ("Due this week", [a for a in rows if a["status"] in C.OPEN and not C.is_overdue(a, NOW)
                                 and (C._epoch(a.get("due_at")) or 0) < NOW + 7 * 86400]),
              ("Later", [a for a in rows if a["status"] in C.OPEN and (C._epoch(a.get("due_at")) or 0) >= NOW + 7 * 86400]),
              ("Done: waiting for the effectiveness check", [a for a in rows if a["status"] == "done" and a["discipline"] == "D6"]),
              ("Not effective", [a for a in rows if a["status"] == "not_effective"]),
              ("Closed", [a for a in rows if a["status"] in ("verified", "cancelled") or (a["status"] == "done" and a["discipline"] != "D6")])]
    for title, items in groups:
        if not items:
            continue
        st.html(f'<div class="sx-label" style="margin:12px 2px 2px">{K.esc(title)} · {len(items)}</div>')
        for a in items:
            action_card(a, "d")
else:
    by_run: dict[str, list] = {}
    for a in rows:
        by_run.setdefault(a["run_id"], []).append(a)
    for rid, items in sorted(by_run.items(), key=lambda kv: kv[0], reverse=True):
        allx = capa_ops.actions(rid)
        stt = C.run_status(allx, NOW)
        done = stt.get("closed", 0)
        pill = {"closed": K.pill("completed", stt["label"], "xs"), "reopened": K.pill("failed", stt["label"], "xs"),
                "verifying": K.pill("done", stt["label"], "xs")}.get(stt["key"], K.pill("running", stt["label"], "xs"))
        with K.card(f"carun_{rid}", accent=stt["overdue"] > 0):
            first = allx[0] if allx else {}
            c1, c2 = st.columns([8, 2], vertical_alignment="center")
            c1.html(f'<div class="sx-row" style="gap:8px;flex-wrap:wrap"><span class="sx-mono" style="font-weight:600">{K.esc(rid)}</span>{pill}'
                    + (f'<span class="sx-pill crit xs">{stt["overdue"]} overdue</span>' if stt["overdue"] else "") +
                    f'</div><div class="sx-muted" style="font-size:12.5px;margin-top:3px">{K.esc(first.get("machine_id") or "")} '
                    f'{K.esc(first.get("machine_name") or "")} · {done} of {stt["total"]} actions closed</div>'
                    f'<div style="margin-top:8px">{K.bar(100 * done / max(stt["total"], 1), True, label="Actions closed")}</div>')
            c2.button("Open report", key=f"carep_{rid}", icon=":material/article:", type="tertiary", on_click=shell.open_report, args=(rid,))
            for a in items:
                action_card(a, f"r{rid[-4:]}")
