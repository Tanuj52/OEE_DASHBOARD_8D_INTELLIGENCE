"""
Corrective-action tracking: the actions in every finished 8D report
(core/capa.py) kept in OPERATIONS.CAPA_ACTIONS with a named owner, a due date,
progress, notes, an audit trail and the effectiveness check that closes or
reopens the 8D.

Reports are read when they finish (automation.on_report_complete) and once for
every stored report when the app connects (backfill), so reports written before
this module existed are tracked too. Action ids are stable, so both are safe to
repeat and two app instances on the same OPERATIONS schema agree.
"""

import statistics
import time

from snowflake_app.core import capa as C, data as D
from snowflake_app.services import ops_store

TABLE = "capa_actions"
TRANSITIONS = {"open": ("in_progress", "done", "cancelled"), "in_progress": ("done", "open", "cancelled"),
               "done": ("verified", "not_effective", "in_progress"), "not_effective": ("in_progress", "cancelled"),
               "verified": ("in_progress",), "cancelled": ("open",)}
_tried: set = set()


def store():
    return ops_store.store()


def _operators() -> list[dict]:
    try:
        return D.records(D.table("operators"))
    except Exception:
        return []


def operators() -> list[dict]:
    return sorted(_operators(), key=lambda o: o["name"])


def actions(run_id: str | None = None) -> list[dict]:
    rows = store().all(TABLE)
    if run_id:
        rows = [a for a in rows if a.get("run_id") == run_id]
    return sorted(rows, key=lambda a: (str(a.get("due_at") or ""), a["action_id"]))


def ingest(snap: dict) -> int:
    """Create the tracked actions for a finished report; returns how many were new."""
    if (snap or {}).get("status") != "completed":
        return 0
    st, n = store(), 0
    for a in C.extract(snap, _operators()):
        if not st.get(TABLE, a["action_id"]):
            a["history"] = [{"at": ops_store.now_iso(), "by": "8D report", "what": f"Created from {snap['id']} ({a['kind'].lower()})"}]
            st.put(TABLE, a)
            n += 1
    return n


def backfill() -> int:
    """Track the actions of stored reports that have none yet (reports finished before tracking existed, or
    imported later). Each report is tried once per backend, so this is cheap to call on every page run."""
    st = store()
    have = {a.get("run_id") for a in st.all(TABLE)}
    n = 0
    for rid in st.keys("investigations"):
        if rid in have or (st.config_key, rid) in _tried:
            continue
        _tried.add((st.config_key, rid))
        n += ingest(st.get("investigations", rid) or {})
    return n


def _change(action_id: str, user: str | None, what: str, **fields) -> dict | None:
    st = store()
    a = st.get(TABLE, action_id)
    if not a:
        return None
    a.update(fields)
    a["history"] = (a.get("history") or []) + [{"at": ops_store.now_iso(), "by": user or "Plant user", "what": what}]
    st.put(TABLE, a)
    return a


def set_status(action_id: str, status: str, user: str | None = None, note: str = "") -> dict | None:
    a = store().get(TABLE, action_id)
    if not a or status not in TRANSITIONS.get(a["status"], ()):
        return None
    now = ops_store.now_iso()
    fields = {"status": status}
    if status == "done":
        fields["done_at"] = now
    elif status in ("verified", "not_effective"):
        fields.update(verified_at=now, result=note or ("Verified by the owner" if status == "verified" else "Marked not effective"))
    elif status in ("in_progress", "open") and a["status"] in ("verified", "not_effective", "done"):
        fields.update(verified_at=None, result=None)  # reopened: the earlier verdict no longer applies
    what = f"{C.STATUS_LABEL[a['status']]} → {C.STATUS_LABEL[status]}" + (f": {note}" if note else "")
    return _change(action_id, user, what, **fields)


def reassign(action_id: str, owner_id: str, user: str | None = None) -> dict | None:
    o = next((x for x in _operators() if x["operator_id"] == owner_id), None)
    a = store().get(TABLE, action_id)
    if not o or not a or a.get("owner_id") == owner_id:
        return None
    return _change(action_id, user, f"Owner {a.get('owner_name') or '—'} → {o['name']}", owner_id=owner_id, owner_name=o["name"])


def set_due(action_id: str, due_iso: str, user: str | None = None) -> dict | None:
    return _change(action_id, user, f"Due date set to {due_iso[:10]}", due_at=due_iso)


def add_note(action_id: str, text: str, user: str | None = None) -> dict | None:
    text = (text or "").strip()
    a = store().get(TABLE, action_id)
    if not text or not a:
        return None
    notes = (a.get("notes") or []) + [{"at": ops_store.now_iso(), "by": user or "Plant user", "text": text[:1000]}]
    return _change(action_id, user, "Note added", notes=notes)


def check_effectiveness(work_orders: list[dict], now: float | None = None) -> int:
    """Close or reopen done corrective actions by the recurrence rule (core/capa.effectiveness)."""
    now = now or time.time()
    n = 0
    for a in actions():
        res = C.effectiveness(a, work_orders, now)
        if res:
            _change(a["action_id"], "Automatic check", f"{C.STATUS_LABEL[res['status']]}: {res['result']}",
                    status=res["status"], verified_at=ops_store.now_iso(), result=res["result"])
            n += 1
    return n


def kpis(now: float | None = None) -> dict:
    now = now or time.time()
    rows = actions()
    opened = [a for a in rows if a["status"] in C.OPEN]
    overdue = [a for a in opened if C.is_overdue(a, now)]
    week = [a for a in opened if not C.is_overdue(a, now) and (C._epoch(a.get("due_at")) or 0) < now + 7 * 86400]
    judged = [a for a in rows if a["status"] in ("verified", "not_effective")]
    days = [max(C._epoch(a["done_at"]) - C._epoch(a["created_at"]), 0) / 86400 for a in rows
            if a.get("done_at") and C._epoch(a.get("created_at"))]
    return {"total": len(rows), "open": len(opened), "overdue": len(overdue), "due_week": len(week),
            "awaiting_verification": sum(1 for a in rows if a["status"] == "done" and a["discipline"] == "D6"),
            "verified_pct": round(100 * sum(1 for a in judged if a["status"] == "verified") / len(judged)) if judged else None,
            "not_effective": sum(1 for a in rows if a["status"] == "not_effective"),
            "median_days_to_close": round(statistics.median(days), 1) if days else None}


def run_status(run_id: str, now: float | None = None) -> dict:
    return C.run_status(actions(run_id), now or time.time())


def statuses(now: float | None = None) -> dict[str, dict]:
    """8D follow-up status for every report that has actions."""
    now = now or time.time()
    by: dict[str, list] = {}
    for a in store().all(TABLE):
        by.setdefault(a.get("run_id"), []).append(a)
    return {rid: C.run_status(rows, now) for rid, rows in by.items()}
