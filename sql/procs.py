"""
Stored-procedure handlers for 8D Intelligence. sql/setup.py zips this file with
the standard-library modules it uses (snowflake_app/core/orderbook.py,
impact.py, digest.py) and uploads them to <db>.OPERATIONS.APP_LIB, so the
procedures run exactly the code the app runs.

  ROLL_ORDER_BOOK(DB)                   keep about four weeks of open orders on every machine
  BUILD_DAILY_DIGEST(DB)                the digest email as {subject, html, counts}
  SEND_DAILY_DIGEST(DB, INTEGRATION)    build it and send it to the lists marked "Daily digest"
"""

import json
import time
import uuid
from datetime import date, datetime


def _rows(session, sql: str, params=None) -> list[dict]:
    return [{k.lower(): v for k, v in r.as_dict().items()} for r in session.sql(sql, params=params).collect()]


def _data(session, sql: str) -> list[dict]:
    out = []
    for r in session.sql(sql).collect():
        v = r[0]
        out.append(json.loads(v) if isinstance(v, str) else v)
    return [x for x in out if x]


def _ident(v: str) -> str:
    v = (v or "").strip().upper()
    if not v.replace("_", "").replace("$", "").isalnum() or v[0].isdigit():
        raise ValueError(f"Not a plain identifier: {v!r}")
    return v


def roll_order_book(session, db: str) -> str:
    from snowflake_app.core import orderbook as OB

    p = f"{_ident(db)}.PRODUCTION"
    machines = _rows(session, f"SELECT machine_id, name, type FROM {p}.MACHINES")
    history = _rows(session, f"SELECT machine_id, product_id, start_date, qty_produced FROM {p}.PRODUCTION_ORDERS")
    products = _rows(session, f"SELECT product_id, unit_price_usd FROM {p}.PRODUCTS")
    customers = _rows(session, f"SELECT customer_id FROM {p}.CUSTOMERS")
    existing = _rows(session, f"SELECT * FROM {p}.ORDER_BOOK")
    res = OB.roll(date.today(), machines, history, products, customers, existing)
    cols = ", ".join(c.upper() for c in OB.COLUMNS)
    casts = {"qty": "NUMBER", "unit_price_usd": "FLOAT", "order_value_usd": "FLOAT", "late_penalty_usd_per_day": "FLOAT",
             "start_date": "DATE", "due_date": "DATE", "released_on": "DATE"}
    sel = ", ".join(f"TRY_CAST(f.value:{c}::STRING AS {casts[c]})" if c in casts else f"f.value:{c}::STRING" for c in OB.COLUMNS)
    payload = json.dumps(res["orders"], default=str)
    session.sql("BEGIN").collect()
    try:
        session.sql(f"DELETE FROM {p}.ORDER_BOOK").collect()
        session.sql(f"INSERT INTO {p}.ORDER_BOOK ({cols}) SELECT {sel} FROM TABLE(FLATTEN(PARSE_JSON(?))) f",
                    params=[payload]).collect()
        session.sql("COMMIT").collect()
    except Exception:
        session.sql("ROLLBACK").collect()
        raise
    open_n = sum(1 for o in res["orders"] if o["status"] != "Shipped")
    return f"{open_n} open orders on {len(machines)} machines: {res['added']} released, {res['shipped']} shipped, {res['dropped']} archived"


def _digest(session, db: str) -> dict:
    from snowflake_app.core import digest as G, orderbook as OB

    db = _ident(db)
    o, p = f"{db}.OPERATIONS", f"{db}.PRODUCTION"
    actions = _data(session, f"SELECT DATA FROM {o}.CAPA_ACTIONS WHERE STATUS IN ('open', 'in_progress')")
    stock = _rows(session, f"SELECT part_id, part_name, on_hand, reserved, available, incoming, reorder_point, vendor_name, "
                           f"lead_time_days FROM {o}.V_PART_STOCK")
    pos = _data(session, f"SELECT DATA FROM {o}.PURCHASE_ORDERS WHERE STATUS IN ('requested', 'approved', 'ordered')")
    states = _data(session, f"SELECT DATA FROM {o}.MACHINE_STATE")
    wos = _data(session, f"SELECT DATA FROM {o}.WORK_ORDERS WHERE STATUS IN ('planned', 'awaiting', 'in_repair', 'commissioning')")
    book = _rows(session, f"SELECT * FROM {p}.ORDER_BOOK WHERE STATUS <> 'Shipped'")
    machines = _rows(session, f"SELECT machine_id, name, type FROM {p}.MACHINES")
    history = _rows(session, f"SELECT machine_id, product_id, start_date, qty_produced FROM {p}.PRODUCTION_ORDERS")
    customers = {r["customer_id"]: r["name"] for r in _rows(session, f"SELECT customer_id, name FROM {p}.CUSTOMERS")}
    rates = {k: v / 24 for k, v in OB.daily_rates(machines, history).items()}
    settings = _data(session, f"SELECT DATA FROM {o}.APP_SETTINGS WHERE NAME = 'plant'")
    values = (settings[0].get("values") if settings else None) or {}
    horizon = float((values.get("automation") or {}).get("plan_hours", 72))
    now = time.time()
    risk = G.risk_from_state(states, wos, book, rates, customers, now, horizon, values.get("business"))
    return G.build(actions, stock, pos, risk, now)


def build_daily_digest(session, db: str) -> dict:
    return _digest(session, db)


def send_daily_digest(session, db: str, integration: str) -> str:
    db, integration = _ident(db), _ident(integration)
    o = f"{db}.OPERATIONS"
    lists = _data(session, f"SELECT DATA FROM {o}.RECIPIENT_LISTS")
    lists = [x for x in lists if x.get("daily_digest") and x.get("emails")]
    if not lists:
        return "No recipient list is marked Daily digest; nothing sent"
    d = _digest(session, db)
    sent = []
    for lst in lists:
        rec = {"email_id": uuid.uuid4().hex[:12], "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "run_id": None, "list_name": lst["name"], "recipients": lst["emails"], "subject": d["subject"][:250],
               "transport": "snowflake", "reason": "daily digest", "status": "sent", "error": None}
        try:
            session.sql("CALL SYSTEM$SEND_EMAIL(?, ?, ?, ?, 'text/html')",
                        params=[integration, ", ".join(lst["emails"]), d["subject"][:250], d["html"]]).collect()
            sent.append(lst["name"])
        except Exception as e:
            rec.update(status="failed", error=str(e).splitlines()[0][:500])
        session.sql(f"INSERT INTO {o}.EMAIL_LOG (EMAIL_ID, SENT_AT, LIST_NAME, RECIPIENTS, SUBJECT, TRANSPORT, STATUS, ERROR, "
                    f"REASON, DATA) SELECT d:email_id::STRING, TRY_TO_TIMESTAMP_TZ(d:sent_at::STRING), d:list_name::STRING, "
                    f"d:recipients::ARRAY, d:subject::STRING, d:transport::STRING, d:status::STRING, d:error::STRING, "
                    f"d:reason::STRING, d FROM (SELECT PARSE_JSON(?) AS d)", params=[json.dumps(rec)]).collect()
    return f"Digest sent to {', '.join(sent) or 'no list'}: {json.dumps(d['counts'])}"
