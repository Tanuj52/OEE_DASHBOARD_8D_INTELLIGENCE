"""
Rolling order book: the customer orders each machine is committed to.

roll() keeps about HORIZON_DAYS of orders ahead on every machine, the way a
planner releases work: orders run back to back, each sized to 80-95 % of what
the machine demonstrably makes in its window (PRODUCTION_ORDERS history), with the
product mix of that machine type and a customer, priority and late penalty.
Orders whose due date has passed are marked Shipped and kept for
KEEP_SHIPPED_DAYS. The result only depends on the existing book and the day,
so running it twice on the same day changes nothing.

Standard library only: the ROLL_ORDER_BOOK stored procedure runs this module
inside Snowflake every day (sql/procs.py), and the app uses it directly when
it reads the bundled files.
"""

import random
from datetime import date, timedelta

HORIZON_DAYS = 28
KEEP_SHIPPED_DAYS = 60
DEFAULT_RATE = 120.0  # units per day when neither the machine nor its type has history
# priority -> (share of orders, late penalty per day as a share of the order value)
PRIORITIES = {"Standard": (0.55, 0.01), "Rush": (0.25, 0.02), "Critical": (0.20, 0.05)}
COLUMNS = ["order_id", "customer_id", "product_id", "machine_id", "qty", "unit_price_usd", "order_value_usd",
           "start_date", "due_date", "priority", "status", "late_penalty_usd_per_day", "released_on"]


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def daily_rates(machines: list[dict], history: list[dict]) -> dict[str, float]:
    """Units per day each machine produced in its order history; machine type, then plant average otherwise."""
    if history:
        days = [_d(h["start_date"]) for h in history]
        span = max((max(days) - min(days)).days + 1, 1)
    else:
        span = 1
    per: dict[str, float] = {}
    for h in history:
        per[h["machine_id"]] = per.get(h["machine_id"], 0.0) + float(h.get("qty_produced") or 0)
    per = {k: v / span for k, v in per.items() if v > 0}
    mtype = {m["machine_id"]: m.get("type") for m in machines}
    by_type: dict[str, list] = {}
    for mid, r in per.items():
        by_type.setdefault(mtype.get(mid), []).append(r)
    plant = sum(per.values()) / len(per) if per else DEFAULT_RATE
    out = {}
    for m in machines:
        mid = m["machine_id"]
        t = by_type.get(m.get("type"))
        out[mid] = round(per.get(mid) or (sum(t) / len(t) if t else plant), 2)
    return out


def product_mix(machines: list[dict], history: list[dict]) -> dict[str, list[tuple[str, int]]]:
    """Products each machine type makes, weighted by how often (plant mix for types without history)."""
    mtype = {m["machine_id"]: m.get("type") for m in machines}
    by_type: dict[str, dict] = {}
    plant: dict[str, int] = {}
    for h in history:
        t = mtype.get(h["machine_id"])
        by_type.setdefault(t, {}).setdefault(h["product_id"], 0)
        by_type[t][h["product_id"]] += 1
        plant[h["product_id"]] = plant.get(h["product_id"], 0) + 1
    return {m["machine_id"]: sorted((by_type.get(m.get("type")) or plant).items()) for m in machines}


def status_on(order: dict, today: date) -> str:
    if _d(order["due_date"]) < today:
        return "Shipped"
    return "In production" if _d(order["start_date"]) <= today else "Open"


def _new_order(mid: str, start: date, rate: float, mix: list, prices: dict, customers: list, today: date) -> dict:
    rng = random.Random(f"{mid}|{start.isoformat()}")
    dur = rng.randint(3, 9)  # days from the start of start_date to the end of due_date
    qty = max(10, int(round(rate * dur * rng.uniform(0.80, 0.95) / 10.0)) * 10)
    names, weights = zip(*mix) if mix else ((sorted(prices) or ["PRODUCT"])[0:1], [1])
    product = rng.choices(list(names), weights=list(weights))[0]
    price = float(prices.get(product, 25.0))
    pri = rng.choices(list(PRIORITIES), weights=[v[0] for v in PRIORITIES.values()])[0]
    value = round(qty * price, 2)
    o = {"order_id": f"SO-{mid.split('-')[-1]}-{start:%y%m%d}", "customer_id": rng.choice(customers) if customers else None,
         "product_id": product, "machine_id": mid, "qty": qty, "unit_price_usd": price, "order_value_usd": value,
         "start_date": start.isoformat(), "due_date": (start + timedelta(days=dur - 1)).isoformat(), "priority": pri,
         "late_penalty_usd_per_day": round(value * PRIORITIES[pri][1], 2), "released_on": today.isoformat()}
    o["status"] = status_on(o, today)
    return o


def roll(today, machines: list[dict], history: list[dict], products: list[dict], customers: list[dict],
         existing: list[dict] | None = None) -> dict:
    """The order book for `today`: {"orders": [...], "added": n, "shipped": n, "dropped": n}."""
    today = _d(today)
    rates = daily_rates(machines, history)
    mixes = product_mix(machines, history)
    prices = {p["product_id"]: float(p["unit_price_usd"]) for p in products}
    custs = sorted(c["customer_id"] for c in customers)
    keep, shipped, dropped = [], 0, 0
    for o in existing or []:
        o = dict(o)
        for k in ("start_date", "due_date", "released_on"):  # DATE columns arrive as date objects
            if o.get(k) is not None:
                o[k] = _d(o[k]).isoformat()
        new = status_on(o, today)
        if new == "Shipped" and _d(o["due_date"]) < today - timedelta(days=KEEP_SHIPPED_DAYS):
            dropped += 1
            continue
        shipped += new == "Shipped" and o.get("status") != "Shipped"
        o["status"] = new
        keep.append(o)
    added = 0
    for m in sorted(machines, key=lambda x: x["machine_id"]):
        mid = m["machine_id"]
        mine = [o for o in keep if o["machine_id"] == mid]
        cursor = max((_d(o["due_date"]) + timedelta(days=1) for o in mine), default=None)
        if cursor is None or cursor < today:  # empty or stale book: the current order started a few days ago
            cursor = today - timedelta(days=random.Random(f"{mid}|{today.isoformat()}").randint(0, 3))
        while cursor < today + timedelta(days=HORIZON_DAYS):
            o = _new_order(mid, cursor, rates.get(mid, DEFAULT_RATE), mixes.get(mid, []), prices, custs, today)
            keep.append(o)
            added += 1
            cursor = _d(o["due_date"]) + timedelta(days=1)
    keep.sort(key=lambda o: (o["machine_id"], o["start_date"]))
    return {"orders": keep, "added": added, "shipped": shipped, "dropped": dropped}
