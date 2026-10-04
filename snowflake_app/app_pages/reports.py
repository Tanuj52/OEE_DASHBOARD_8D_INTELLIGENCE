from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from snowflake_app.services import capa_ops, report_export as X
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/reports.py"
shell.topbar(PATH)
rm = shell.run_manager()
ss = st.session_state

PAGE_SIZE = 12
TRIGGERS = {"live": "Live monitoring", "fleet": "Fleet scan", "auto": "Fleet scan", "machine": "Machine"}
RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PERIODS = {"any": "Any time", "today": "Today", "7d": "Last 7 days", "30d": "Last 30 days"}
SORTS = {"new": "Newest first", "old": "Oldest first", "risk": "Highest risk first", "machine": "Machine A–Z"}

for k, v in (("rep_q", ""), ("rep_status", "all"), ("rep_line", "all"), ("rep_period", "any"), ("rep_sort", "new"),
             ("rep_view", "cards"), ("rep_page", 0)):
    ss.setdefault(k, v)

runs = [r for r in rm.list() if r["status"] in ("completed", "cancelled", "failed")]
FOLLOW = capa_ops.statuses()
if not runs:
    with K.card("empty"):
        st.html('<div style="text-align:center;padding:40px 10px"><div style="font-weight:600;font-size:15px">No reports yet</div>'
                '<div class="sx-subtle" style="font-size:12.5px;margin-top:4px">Reports are generated when an 8D investigation finishes.</div></div>')
        st.button("Start an investigation", icon=":material/account_tree:", type="primary",
                  on_click=shell.goto, args=("app_pages/investigations.py",))
    st.stop()


def created(r) -> datetime:
    return datetime.fromisoformat(r["created_at"])


# ------------------------------------------------------------------- overview
machines = {r["trigger"].get("machine_id") for r in runs if r["trigger"].get("machine_id")}
escalated = sum(1 for r in runs if (r.get("decision") or {}).get("escalate"))
done = sum(1 for r in runs if r["status"] == "completed")
latest = max(runs, key=created)
week = sum(1 for r in runs if created(r) >= datetime.now() - timedelta(days=7))
when = created(latest)
st.html('<div class="sx-kpis">' + "".join([
    K.kpi("Reports", K.num(len(runs)), hint=f"{week} this week", ico="file"),
    K.kpi("Escalated to 8D", K.num(escalated), hint="PdM decision", ico="alert", emphasis=escalated > 0),
    K.kpi("Machines covered", K.num(len(machines)), hint=", ".join(sorted(machines)) or "—", ico="cpu"),
    K.kpi("Latest report", f"{when:%-I:%M %p}" if when.date() == date.today() else f"{when:%b %-d}",
          hint=f'{latest["trigger"].get("machine_id") or "Plant"} · {"today" if when.date() == date.today() else f"{when:%Y}"}', ico="clock"),
]) + "</div>")
st.space("small")


# -------------------------------------------------------------------- toolbar
def _reset_page():
    ss.rep_page = 0


def _clear_filters():
    for k, v in (("rep_q", ""), ("rep_status", "all"), ("rep_line", "all"), ("rep_period", "any")):
        ss[k] = v
        ss.pop(f"w_{k}", None)
    ss.rep_page = 0


lines = sorted({r["trigger"].get("line") for r in runs if r["trigger"].get("line")})
with K.card("toolbar"):
    c = st.columns([6, 2.4, 1.6], vertical_alignment="bottom")
    with c[0]:
        shell.synced(st.text_input, "Search", state="rep_q", after=_reset_page, icon=":material/search:",
                     placeholder="Report ID, machine, line, sensor or outcome")
    with c[1]:
        shell.synced(st.selectbox, "Sort", list(SORTS), state="rep_sort", after=_reset_page, format_func=SORTS.get)
    with c[2]:
        shell.synced(st.segmented_control, "View", ["cards", "table"], state="rep_view", width="stretch",
                     format_func=lambda v: ":material/view_agenda:" if v == "cards" else ":material/table_rows:")
    c = st.columns(3)
    with c[0]:
        shell.synced(st.selectbox, "Status", ["all", "completed", "cancelled", "failed"], state="rep_status", after=_reset_page,
                     format_func=lambda v: "All statuses" if v == "all" else v.capitalize())
    with c[1]:
        shell.synced(st.selectbox, "Line", ["all"] + lines, state="rep_line", after=_reset_page,
                     format_func=lambda v: "All lines" if v == "all" else v)
    with c[2]:
        shell.synced(st.selectbox, "Period", list(PERIODS), state="rep_period", after=_reset_page, format_func=PERIODS.get)


def haystack(r) -> str:
    t = r["trigger"]
    return " ".join(str(x or "") for x in (r["id"], t.get("machine_id"), t.get("machine_name"), t.get("line"), t.get("sensor"),
                                            K.sensor_label(t.get("sensor")) if t.get("sensor") else "", t.get("risk"),
                                            (r.get("decision") or {}).get("message"), r["status"])).lower()


now = datetime.now()
cut = {"any": None, "today": datetime.combine(date.today(), datetime.min.time()),
       "7d": now - timedelta(days=7), "30d": now - timedelta(days=30)}[ss.rep_period]
terms = ss.rep_q.lower().split()
rows = [r for r in runs
        if (ss.rep_status == "all" or r["status"] == ss.rep_status)
        and (ss.rep_line == "all" or r["trigger"].get("line") == ss.rep_line)
        and (cut is None or created(r) >= cut)
        and all(tm in haystack(r) for tm in terms)]
if ss.rep_sort == "old":
    rows.sort(key=created)
elif ss.rep_sort == "risk":
    rows.sort(key=lambda r: (RISK_ORDER.get(r["trigger"].get("risk"), 9), -created(r).timestamp()))
elif ss.rep_sort == "machine":
    rows.sort(key=lambda r: (r["trigger"].get("machine_id") or "", -created(r).timestamp()))
else:
    rows.sort(key=created, reverse=True)

filtered = len(rows) != len(runs)
with st.container(horizontal=True, vertical_alignment="center"):
    st.html(f'<div class="sx-muted" style="font-size:13px"><b style="color:#1B1816">{len(rows)}</b> of {len(runs)} reports'
            + (" match the filters" if filtered else "") + "</div>")
    if filtered:
        st.button("Clear filters", icon=":material/filter_alt_off:", type="tertiary", key="rep_clear", on_click=_clear_filters)

if not rows:
    with K.card("nomatch"):
        st.html('<div style="text-align:center;padding:28px 10px"><div style="font-weight:600">No reports match</div>'
                '<div class="sx-subtle" style="font-size:12.5px;margin-top:4px">Try a different search or clear the filters.</div></div>')
    st.stop()


# ---------------------------------------------------------------------- cards
def day_label(d: date) -> str:
    if d == date.today():
        return "Today"
    if d == date.today() - timedelta(days=1):
        return "Yesterday"
    return f"{d:%A, %B %-d, %Y}"


def _pdf_for(run_id: str):
    return lambda: X.to_pdf(rm.get(run_id))


def follow_html(run_id: str) -> str:
    f = FOLLOW.get(run_id)
    if not f:
        return '<span class="sx-subtle">No tracked actions</span>'
    variant = {"closed": "completed", "reopened": "failed", "verifying": "done"}.get(f["key"], "running")
    late = f' <span class="sx-pill crit xs">{f["overdue"]} overdue</span>' if f["overdue"] else ""
    return f'{K.pill(variant, f["label"], "xs")}{late}'


def report_row(r: dict):
    t = r["trigger"]
    dec = r.get("decision") or {}
    sig = (f'{K.sensor_label(t["sensor"])} {K.sensor_val(t["sensor"], t.get("value"))} {K.sensor_unit(t["sensor"])}'
           if t.get("sensor") else "Fleet-wide scan")
    with K.card(f"rep_{r['id']}", accent=r["status"] == "completed" and t.get("risk") == "critical"):
        c1, c2, c3 = st.columns([4.4, 3.8, 3], vertical_alignment="center", gap="medium")
        c1.html(
            f'<div class="sx-row" style="gap:8px"><span class="sx-mono sx-subtle" style="font-size:11.5px;white-space:nowrap">{K.esc(r["id"])}</span>'
            f'{K.pill(r["status"], size="xs")}</div>'
            f'<div style="font-size:15px;font-weight:600;margin-top:5px;line-height:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'{K.esc(t.get("machine_id") or "Plant")} <span style="font-weight:500">{K.esc(t.get("machine_name") or "")}</span></div>'
            f'<div class="sx-muted tabular" style="font-size:12.5px;margin-top:1px">{created(r):%-I:%M %p} · {K.esc(t.get("line") or "—")} · '
            f'{K.esc(TRIGGERS.get(t.get("source"), (t.get("source") or "").capitalize()))}</div>')
        c2.html(
            f'<div style="display:grid;grid-template-columns:62px 1fr;gap:5px 8px;font-size:12.5px;align-items:center">'
            f'<span class="sx-subtle">Signal</span><span class="sx-row" style="gap:6px;flex-wrap:wrap"><b style="font-weight:500">{K.esc(sig)}</b>'
            f'{K.pill(t["risk"], size="xs") if t.get("risk") else ""}</span>'
            f'<span class="sx-subtle">Outcome</span><span>{K.esc(dec.get("message") or "—")}</span>'
            f'<span class="sx-subtle">Follow-up</span><span>{follow_html(r["id"])}</span>'
            f'<span class="sx-subtle">Agents</span><span class="tabular sx-muted">{r["progress"]["done"]}/{r["progress"]["total"]} · '
            f'{K.num(r.get("total_tokens"))} tokens</span></div>')
        with c3.container(horizontal=True, horizontal_alignment="right", gap="small"):
            st.download_button("PDF", _pdf_for(r["id"]), file_name=f"{X.file_stem(r)}.pdf", mime="application/pdf",
                               icon=":material/picture_as_pdf:", key=f"pdf_{r['id']}", on_click="ignore", help="Download PDF")
            st.button("Open", icon=":material/arrow_forward:", icon_position="right", key=f"open_{r['id']}", type="primary",
                      on_click=shell.open_report, args=(r["id"],))


def _page(delta: int, pages: int):
    ss.rep_page = max(0, min(pages - 1, ss.rep_page + delta))


if ss.rep_view == "cards":
    pages = (len(rows) - 1) // PAGE_SIZE + 1
    ss.rep_page = min(ss.rep_page, pages - 1)
    chunk = rows[ss.rep_page * PAGE_SIZE:(ss.rep_page + 1) * PAGE_SIZE]
    groups: dict[date, list] = {}
    for r in chunk:
        groups.setdefault(created(r).date(), []).append(r)
    for d, items in groups.items():
        st.html(f'<div class="sx-label" style="margin:10px 2px 2px">{K.esc(day_label(d))} · {len(items)}</div>')
        for r in items:
            report_row(r)
    if pages > 1:
        with st.container(horizontal=True, horizontal_alignment="center", vertical_alignment="center"):
            st.button("Previous", icon=":material/chevron_left:", key="rep_prev", disabled=ss.rep_page == 0,
                      on_click=_page, args=(-1, pages))
            st.html(f'<div class="sx-muted tabular" style="font-size:12.5px;padding:0 10px">Page {ss.rep_page + 1} of {pages} · '
                    f'{ss.rep_page * PAGE_SIZE + 1}–{ss.rep_page * PAGE_SIZE + len(chunk)} of {len(rows)}</div>')
            st.button("Next", icon=":material/chevron_right:", icon_position="right", key="rep_next",
                      disabled=ss.rep_page >= pages - 1, on_click=_page, args=(1, pages))

# ---------------------------------------------------------------------- table
else:
    df = pd.DataFrame([{
        "id": r["id"], "created": created(r), "machine": r["trigger"].get("machine_id"), "name": r["trigger"].get("machine_name"),
        "line": r["trigger"].get("line"), "trigger": TRIGGERS.get(r["trigger"].get("source"), r["trigger"].get("source")),
        "risk": (r["trigger"].get("risk") or "").capitalize(), "status": r["status"].capitalize(),
        "outcome": (r.get("decision") or {}).get("message"), "agents": f'{r["progress"]["done"]}/{r["progress"]["total"]}',
        "follow": (FOLLOW.get(r["id"]) or {}).get("label", "—"), "overdue": (FOLLOW.get(r["id"]) or {}).get("overdue", 0),
        "tokens": r.get("total_tokens"),
    } for r in rows])

    def _open_selected():
        sel = ss.rep_table.selection.rows
        if sel:
            shell.open_report(df.iloc[sel[0]]["id"])

    with K.card("table"):
        st.html(K.card_title("All reports", "Select a row to open the report. Click a column header to sort.", "list"))
        st.dataframe(df, hide_index=True, on_select=_open_selected, selection_mode="single-row", key="rep_table",
                     height=min(36 * (len(df) + 1) + 4, 640),
                     column_config={"id": st.column_config.TextColumn("Report", width="medium"),
                                    "created": st.column_config.DatetimeColumn("Created", format="MMM D, h:mm a"),
                                    "machine": "Machine", "name": "Name", "line": "Line", "trigger": "Trigger", "risk": "Risk",
                                    "status": "Status", "outcome": st.column_config.TextColumn("Outcome", width="large"),
                                    "follow": "8D follow-up", "overdue": st.column_config.NumberColumn("Overdue actions"),
                                    "agents": "Agents", "tokens": st.column_config.NumberColumn("Tokens", format="localized")})
