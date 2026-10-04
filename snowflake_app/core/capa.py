"""
Corrective and preventive actions (CAPA) from a finished 8D report.

The agents write fixed Markdown sections, so actions are read from the report
itself (no extra model calls, works for every stored report):

  D3  Containment actions          table: action | owner role | due
  D5  Corrective actions           table: action | addresses | effectiveness | cost | risk
  D6  Implementation plan          table: task | owner role | start | due
  D7  Systemic changes             bullet list (+ horizontal deployment to similar machines)

D6 tasks are the D5 actions put into a plan, so D5 rows only become actions when
there is no D6 table; otherwise they add what each D6 task addresses. Owners
are named people: the D1 team table names them per role, the plant operators
(OPERATORS certifications) fill roles the team does not cover. Due dates are
read from the report ("2 hours", "Day 3", "1 week", "2026-10-12", ...) relative
to when the report finished. Action ids are stable, so reading a report again
creates nothing new.

Effectiveness: a D6 action that is done is verified effective when the
machine has no new breakdown on the same signal for VERIFY_DAYS; a breakdown in
that time marks it not effective (the 8D is reopened).
"""

import hashlib
import re
from datetime import datetime, timedelta

from snowflake_app.services.report_export import parse_blocks, section_markdown

VERIFY_DAYS = 14
OPEN = ("open", "in_progress")
CLOSED = ("verified", "cancelled")
STATUS_LABEL = {"open": "Open", "in_progress": "In progress", "done": "Done", "verified": "Verified effective",
                "not_effective": "Not effective", "cancelled": "Cancelled"}
DISCIPLINE = {"D3": "Containment", "D6": "Corrective", "D7": "Preventive"}
DEFAULT_DUE_H = {"D3": 24, "D6": 14 * 24, "D7": 30 * 24}
_OP = re.compile(r"\bOP-\d{3}\b")
_UNIT = {"h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1, "d": 24, "day": 24, "days": 24, "w": 168, "wk": 168,
         "week": 168, "weeks": 168, "month": 720, "months": 720, "shift": 8, "shifts": 8, "min": 1 / 60, "mins": 1 / 60,
         "minutes": 1 / 60}
_SYN = {"quality": {"quality", "qa", "inspection", "inspector", "metrology"},
        "maintenance": {"maintenance", "technician", "reliability", "mechanic", "repair"},
        "production": {"production", "operations", "operator", "supervisor", "shift", "line"},
        "lead": {"lead", "leader", "manager", "champion", "coordinator"},
        "supplier": {"supplier", "purchasing", "procurement", "buyer"},
        "engineering": {"engineer", "engineering", "process", "design"},
        "training": {"training", "trainer", "hr"}}
_CERT = {"quality": "Quality", "maintenance": "Maintenance"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\*\*|__|`", "", text or "")).strip().strip("*").strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _clean(text).lower())


def _tokens(text: str) -> set:
    return {w for w in _norm(text).split() if len(w) > 2}


def table_rows(output: str | None, heading: str) -> list[dict]:
    """Rows of the first table in the ### section starting with `heading`, keyed by lower-case header."""
    for b in parse_blocks(section_markdown(output, heading)):
        if b["t"] == "table":
            head = [_norm(h) for h in b["header"]]
            return [dict(zip(head, (_clean(c) for c in r))) for r in b["rows"]
                    if any(_clean(c) for c in r) and _norm(r[0]) not in head]
    return []


def bullets(output: str | None, heading: str) -> list[str]:
    out = []
    for b in parse_blocks(section_markdown(output, heading)):
        if b["t"] == "list":
            out += [_clean(t) for d, t in b["items"] if d == 0 and len(_clean(t)) > 8]
    return out


def _col(row: dict, *names) -> str:
    for k, v in row.items():
        if any(n in k for n in names):
            return v
    return ""


def parse_due(text: str, base: datetime, discipline: str, anchor: datetime | None = None) -> datetime:
    """Absolute due time from what the report says, relative to `base` (when the report finished).

    A calendar date before the report (the agents sometimes date a plan in the period of the data they
    read) keeps its spacing: it is moved so the earliest such date (`anchor`) falls on the default due time."""
    t = _norm(text)
    default = base + timedelta(hours=DEFAULT_DUE_H.get(discipline, 168))
    if not t or t in ("tbd", "na", "ongoing", "none"):
        return default
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if m:
        try:
            d = datetime(int(m[1]), int(m[2]), int(m[3]), 17, 0, tzinfo=base.tzinfo)
        except ValueError:
            return default
        if d.date() < base.date():
            return default + (d - (anchor or d))
        return d
    if any(w in t for w in ("immediate", "asap", "right away")) or t == "now":
        return base + timedelta(hours=1)
    m = re.search(r"\b(day|week|month|shift)\s*(\d+)\b", t)
    if m:
        return base + timedelta(hours=int(m[2]) * _UNIT[m[1]])
    m = re.search(r"(\d+(?:\.\d+)?)\s*(minutes|mins|min|hours|hour|hrs|hr|h|days|day|d|weeks|week|wk|w|months|month|shifts|shift)\b", t)
    if m:
        return base + timedelta(hours=float(m[1]) * _UNIT[m[2]])
    if "end of" in t and "month" in t:
        nxt = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        return nxt.replace(hour=17, minute=0, second=0) - timedelta(days=1)
    if "end of" in t and "week" in t:
        return (base + timedelta(days=(4 - base.weekday()) % 7)).replace(hour=17, minute=0, second=0)
    if "today" in t or "end of day" in t or "end of shift" in t:
        return base.replace(hour=max(base.hour, 17), minute=0, second=0)
    if "tomorrow" in t:
        return (base + timedelta(days=1)).replace(hour=17, minute=0, second=0)
    if "next shift" in t:
        return base + timedelta(hours=8)
    return default


def team(snap: dict) -> list[dict]:
    """D1 team: [{role, name, id}] from the team table, ids resolved from the operators the D1 tool found."""
    steps = {s["id"]: s for s in snap.get("steps") or []}
    d1 = steps.get("d1") or {}
    known = {}
    for tl in d1.get("tools") or []:
        for o in (tl.get("result") or {}).get("operators") or []:
            known[o["operator_id"]] = o
    out = []
    for r in table_rows(d1.get("output"), "team members"):
        person = _col(r, "person", "name", "member")
        m = _OP.search(person)
        name = _clean(re.sub(r"\(.*?\)", "", person))
        oid = m.group(0) if m else next((k for k, o in known.items() if o["name"].lower() == name.lower()), None)
        if name or oid:
            out.append({"role": _clean(_col(r, "role")), "name": name or (known.get(oid) or {}).get("name"), "id": oid})
    return out


def _families(text: str) -> set:
    toks = _tokens(text)
    return {f for f, words in _SYN.items() if toks & words}


def assign_owner(role_text: str, crew: list[dict], operators: list[dict], load: dict) -> dict:
    """Named owner for a role written in the report: an explicit OP-id, then the best-matching D1 role,
    then an operator certified for it, then the team lead. `load` spreads ties across people."""
    ops = {o["operator_id"]: o for o in operators}
    role = _clean(re.sub(r"\(.*?\)", "", role_text or "")) or "Team member"
    m = _OP.search(role_text or "")
    if m and m.group(0) in ops:
        return {"owner_id": m.group(0), "owner_name": ops[m.group(0)]["name"], "owner_role": role}
    want = _families(role)
    best, score = None, 0
    for member in crew:
        s = len(_families(member["role"]) & want) * 3 + len(_tokens(member["role"]) & _tokens(role))
        if s > score or (s == score and s and best and load.get(member["id"], 0) < load.get(best["id"], 0)):
            best, score = member, s
    if best and score and best.get("id"):
        return {"owner_id": best["id"], "owner_name": best["name"], "owner_role": role}
    for fam, cert in _CERT.items():
        if fam in want:
            cands = [o for o in operators if cert.lower() in str(o.get("certifications") or "").lower()]
            if cands:
                # a dedicated role ("Quality Level 3") before a secondary tag ("CNC Level 3,Quality"), then the lightest load
                lvl = lambda o: max((int(m[1]) for m in re.finditer(rf"{cert} Level (\d)", str(o.get("certifications") or ""))), default=0)
                o = min(cands, key=lambda o: (load.get(o["operator_id"], 0), -lvl(o), o["operator_id"]))
                return {"owner_id": o["operator_id"], "owner_name": o["name"], "owner_role": role}
    lead = next((c for c in crew if "lead" in _families(c["role"]) and c.get("id")), None) or next((c for c in crew if c.get("id")), None)
    if lead:
        return {"owner_id": lead["id"], "owner_name": lead["name"], "owner_role": role}
    o = min(operators, key=lambda o: (load.get(o["operator_id"], 0), o["operator_id"])) if operators else None
    return {"owner_id": o and o["operator_id"], "owner_name": o and o["name"], "owner_role": role}


def action_id(run_id: str, discipline: str, text: str) -> str:
    return "CA-" + hashlib.sha1(f"{run_id}|{discipline}|{_norm(text)}".encode()).hexdigest()[:10].upper()


def _similar(a: str, b: str) -> float:
    x, y = _tokens(a), _tokens(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def extract(snap: dict, operators: list[dict]) -> list[dict]:
    """Action records for a finished report (empty when the 8D sections did not run)."""
    steps = {s["id"]: s for s in snap.get("steps") or [] if s.get("status") == "done"}
    if not any(k in steps for k in ("d3", "d5", "d6", "d7")):
        return []
    trig = snap.get("trigger") or {}
    try:
        base = datetime.fromisoformat(snap.get("finished_at") or snap.get("created_at"))
    except (TypeError, ValueError):
        base = datetime.now()
    if base.tzinfo is None:
        base = base.astimezone()
    crew, load, rows = team(snap), {}, []
    anchors: dict[str, datetime] = {}

    def anchor_for(disc: str, dues: list[str]):
        past = []
        for d in dues:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", d or "")
            try:
                if m and datetime(int(m[1]), int(m[2]), int(m[3])).date() < base.date():
                    past.append(datetime(int(m[1]), int(m[2]), int(m[3]), 17, 0, tzinfo=base.tzinfo))
            except ValueError:
                pass
        if past:
            anchors[disc] = min(past)

    def add(disc, text, role, due, **extra):
        text = _clean(text).rstrip(".")
        if len(text) < 4:
            return
        aid = action_id(snap["id"], disc, text)
        if any(r["action_id"] == aid for r in rows):
            return
        owner = assign_owner(role, crew, operators, load)
        load[owner["owner_id"]] = load.get(owner["owner_id"], 0) + 1
        rows.append({"action_id": aid, "run_id": snap["id"], "machine_id": trig.get("machine_id"),
                     "machine_name": trig.get("machine_name"), "line": trig.get("line"), "sensor": trig.get("sensor"),
                     "discipline": disc, "kind": DISCIPLINE[disc], "action": text, **owner,
                     "due_text": due, "due_at": parse_due(due, base, disc, anchors.get(disc)).isoformat(timespec="seconds"),
                     "status": "open", "created_at": base.isoformat(timespec="seconds"), "done_at": None,
                     "verified_at": None, "result": None, "notes": [], "history": [], **extra})

    d3 = table_rows((steps.get("d3") or {}).get("output"), "containment actions")
    anchor_for("D3", [_col(r, "due", "when", "deadline") for r in d3])
    for r in d3:
        add("D3", _col(r, "action", "task"), _col(r, "owner", "role", "who"), _col(r, "due", "when", "deadline"))
    d5 = table_rows((steps.get("d5") or {}).get("output"), "corrective actions")
    d6 = table_rows((steps.get("d6") or {}).get("output"), "implementation plan")
    anchor_for("D6", [_col(r, "due", "end", "deadline") for r in d6])
    if d6:
        for r in d6:
            task = _col(r, "task", "action")
            match = max(d5, key=lambda x: _similar(task, _col(x, "action")), default=None)
            m5 = match if match and _similar(task, _col(match, "action")) >= 0.4 else {}
            add("D6", task, _col(r, "owner", "role", "who"), _col(r, "due", "end", "deadline"),
                addresses=_col(m5, "address"), effectiveness=_col(m5, "effective"), cost=_col(m5, "cost"))
    else:
        for r in d5:
            if _norm(_col(r, "effective")) in ("low",):
                continue
            add("D6", _col(r, "action"), "Maintenance", "", addresses=_col(r, "address"),
                effectiveness=_col(r, "effective"), cost=_col(r, "cost"))
    d7 = steps.get("d7") or {}
    for b in bullets(d7.get("output"), "systemic changes"):
        add("D7", b, "Team lead", "")
    peers = [m["machine_id"] for tl in d7.get("tools") or [] for m in (tl.get("result") or {}).get("similar_machines") or []]
    if peers and d6:
        add("D7", f"Deploy the corrective actions to similar equipment: {', '.join(peers)}", "Maintenance", "30 days")
    return rows


# ------------------------------------------------------------------ follow-up
def _epoch(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp() if iso else None
    except ValueError:
        return None


def is_overdue(a: dict, now: float) -> bool:
    due = _epoch(a.get("due_at"))
    return a.get("status") in OPEN and due is not None and due < now


def effectiveness(a: dict, work_orders: list[dict], now: float, window_days: int = VERIFY_DAYS) -> dict | None:
    """For a done D6 action: {"status": "verified" | "not_effective", "result": text}, or None while verifying."""
    if a.get("status") != "done" or a.get("discipline") != "D6" or not a.get("machine_id"):
        return None
    done = _epoch(a.get("done_at"))
    if done is None:
        return None
    for w in sorted(work_orders, key=lambda w: str(w.get("created_at") or "")):
        created = _epoch(w.get("created_at"))
        same = not a.get("sensor") or w.get("sensor") in (None, a.get("sensor"))
        if (w.get("machine_id") == a["machine_id"] and w.get("kind") == "unplanned" and created and created > done
                and same and w.get("status") != "cancelled"):
            return {"status": "not_effective",
                    "result": f"Recurred: breakdown {w['wo_id']} on {datetime.fromtimestamp(created):%b %-d} after the action was done"}
    if now - done >= window_days * 86400:
        return {"status": "verified", "result": f"No breakdown on {a['machine_id']} in the {window_days} days after the action"}
    return None


def run_status(actions: list[dict], now: float) -> dict:
    """8D follow-up status for one report's actions."""
    if not actions:
        return {"key": "none", "label": "No actions", "open": 0, "overdue": 0, "total": 0}
    st = [a["status"] for a in actions]
    overdue = sum(1 for a in actions if is_overdue(a, now))
    opened = sum(1 for s in st if s in OPEN)
    closed = sum(1 for a in actions if a["status"] in CLOSED or (a["status"] == "done" and a["discipline"] != "D6"))
    if "not_effective" in st:
        key, label = "reopened", "Reopened: not effective"
    elif opened:
        key, label = "open", f"Actions open ({opened})"
    elif closed == len(actions):
        key, label = "closed", "Closed"
    else:
        key, label = "verifying", "Verifying effectiveness"
    return {"key": key, "label": label, "open": opened, "overdue": overdue, "total": len(actions), "closed": closed}
