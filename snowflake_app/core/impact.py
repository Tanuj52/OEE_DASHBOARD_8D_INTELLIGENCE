"""
Business impact of downtime: which customer orders a stop puts at risk, what
an hour of downtime costs, the best time for a planned job and which machine
could take the work over.

The model is a machine's open orders run earliest-due-first at its
demonstrated rate (units/hour from the order history), never before an
order's start date. An outage is a window in which the machine makes nothing.
Comparing the schedule with and without the outage shows which orders slip,
which become late (finish after the end of their due date) and the late
penalties they incur. Downtime cost per hour is the contribution margin of the
output lost plus the idle crew.

Standard library only: the app and the daily digest stored procedure in
Snowflake (sql/procs.py) use the same code.
"""

import math
from datetime import datetime

OPEN = ("Open", "In production")
DEFAULTS = {"margin_pct": 35.0, "labour_usd_h": 45.0, "crew": 1, "tight_hours": 8.0}


def _start_ts(o: dict) -> float:
    return datetime.fromisoformat(str(o["start_date"])[:10]).timestamp()


def _due_ts(o: dict) -> float:
    return datetime.fromisoformat(str(o["due_date"])[:10]).timestamp() + 86400  # end of the due day


def open_orders(book: list[dict], machine_id: str) -> list[dict]:
    return sorted((o for o in book if o["machine_id"] == machine_id and o.get("status") in OPEN),
                  key=lambda o: (str(o["due_date"]), str(o["start_date"]), o["order_id"]))


def _advance(t: float, need_s: float, outages) -> float:
    """Finish time of need_s seconds of work started at t, skipping the outage windows."""
    for s, e in outages:
        if e <= t:
            continue
        if s > t:
            if need_s <= s - t:
                return t + need_s
            need_s -= s - t
        t = max(t, e)
    return t + need_s


def simulate(orders: list[dict], rate_h: float, outages=()) -> dict[str, dict]:
    """{order_id: {finish, slack_h}} for orders run back to back at rate_h units/hour."""
    rate = max(float(rate_h or 0), 0.1)
    outs = sorted((float(s), float(e)) for s, e in outages)
    t, out = None, {}
    for o in orders:
        start = _start_ts(o) if t is None else max(t, _start_ts(o))
        t = _advance(start, float(o["qty"]) / rate * 3600, outs)
        out[o["order_id"]] = {"finish": t, "slack_h": (_due_ts(o) - t) / 3600}
    return out


def _late_days(h: float) -> int:
    return math.ceil(h / 24) if h > 0.01 else 0


def outage_impact(orders: list[dict], rate_h: float, start: float, hours: float, cfg: dict | None = None,
                  customers: dict | None = None) -> dict:
    """Orders affected by a stop of `hours` from epoch `start`, against the schedule without it."""
    cfg = {**DEFAULTS, **(cfg or {})}
    base = simulate(orders, rate_h)
    scen = simulate(orders, rate_h, [(start, start + max(hours, 0) * 3600)])
    rows = []
    for o in orders:
        b, s = base[o["order_id"]], scen[o["order_id"]]
        delay = (s["finish"] - b["finish"]) / 3600
        if delay <= 0.01:
            continue
        late_h = max(-s["slack_h"], 0.0)
        added = late_h - max(-b["slack_h"], 0.0)
        status = "late" if s["slack_h"] < 0 else ("tight" if s["slack_h"] < cfg["tight_hours"] else "delayed")
        added_days = _late_days(late_h) - _late_days(max(-b["slack_h"], 0.0))
        rows.append({**o, "customer_name": (customers or {}).get(o.get("customer_id"), o.get("customer_id")),
                     "delay_h": round(delay, 2), "slack_h": round(s["slack_h"], 1), "late_h": round(late_h, 1),
                     "added_late_h": round(max(added, 0), 1), "finish_at": s["finish"], "status": status,
                     "penalty_usd": round(max(added_days, 0) * float(o.get("late_penalty_usd_per_day") or 0), 2)})
    late = [r for r in rows if r["status"] == "late" and r["added_late_h"] > 0]
    return {"orders": rows, "late": late, "late_count": len(late),
            "tight_count": sum(1 for r in rows if r["status"] == "tight"),
            "value_at_risk": round(sum(float(r["order_value_usd"]) for r in late), 2),
            "penalty_usd": round(sum(r["penalty_usd"] for r in late), 2),
            "customers": sorted({r["customer_name"] for r in late if r.get("customer_name")})}


def cost_per_hour(orders: list[dict], rate_h: float, cfg: dict | None = None) -> dict:
    """Contribution margin of the output a stopped machine does not make, plus the idle crew."""
    cfg = {**DEFAULTS, **(cfg or {})}
    qty = sum(float(o["qty"]) for o in orders)
    price = sum(float(o["order_value_usd"]) for o in orders) / qty if qty else 0.0
    margin = float(rate_h or 0) * price * float(cfg["margin_pct"]) / 100 if orders else 0.0
    labour = float(cfg["labour_usd_h"]) * float(cfg["crew"])
    return {"lost_margin_h": round(margin, 2), "labour_h": round(labour, 2), "total_h": round(margin + labour, 2),
            "avg_price": round(price, 2), "loaded": bool(orders)}


def stop_cost(orders: list[dict], rate_h: float, start: float, hours: float, repair_cost: float = 0.0,
              cfg: dict | None = None, customers: dict | None = None) -> dict:
    """Everything a stop costs: lost margin and idle crew for its hours, the repair, and late penalties."""
    imp = outage_impact(orders, rate_h, start, hours, cfg, customers)
    per_h = cost_per_hour(orders, rate_h, cfg)
    total = per_h["total_h"] * hours + float(repair_cost or 0) + imp["penalty_usd"]
    return {**imp, "hours": round(hours, 2), "cost_per_h": per_h, "repair_cost": round(float(repair_cost or 0), 2),
            "downtime_cost": round(per_h["total_h"] * hours, 2), "total_cost": round(total, 2)}


def best_window(orders: list[dict], rate_h: float, now: float, est_h: float, deadline_h: float | None,
                cfg: dict | None = None, step_h: float = 1.0, horizon_h: float = 72.0) -> dict:
    """Start time for a planned job of est_h hours, between now and the forecast failure, that puts the
    fewest orders at risk: fewest late penalties, then fewest late orders, then the most slack."""
    end = (deadline_h if deadline_h is not None else horizon_h) - est_h
    n = max(int(max(end, 0) // step_h), 0)
    best = None
    for i in range(min(n, 336) + 1):
        start = now + i * step_h * 3600
        imp = outage_impact(orders, rate_h, start, est_h, cfg)
        slack = min((r["slack_h"] for r in imp["orders"]), default=999.0)
        score = (imp["penalty_usd"], imp["late_count"], imp["tight_count"], -slack)
        if best is None or score < best[0]:
            best = (score, i * step_h, imp)
        if score[:3] == (0, 0, 0) and not imp["orders"]:
            break  # an idle gap: nothing is delayed at all
    score, offset, imp = best
    return {"start_in_h": offset, "start_at": now + offset * 3600, "late_count": imp["late_count"],
            "tight_count": imp["tight_count"], "penalty_usd": imp["penalty_usd"], "value_at_risk": imp["value_at_risk"],
            "delayed": len(imp["orders"]), "fits_before_failure": deadline_h is None or end >= 0}


def spare_hours(orders: list[dict], rate_h: float, now: float, days: float = 7.0) -> float:
    """Free machine hours in the next `days`: the hours its open orders do not need."""
    rate = max(float(rate_h or 0), 0.1)
    window_end = now + days * 86400
    sim = simulate(orders, rate)
    busy = 0.0
    for o in orders:
        f = sim[o["order_id"]]["finish"]
        need = float(o["qty"]) / rate * 3600
        s = f - need
        busy += max(min(f, window_end) - max(s, now), 0)
    return round(max(days * 24 - busy / 3600, 0), 1)


def alternates(machine: dict, machines: list[dict], book: list[dict], rates: dict, now: float, need_h: float,
               unavailable: set) -> list[dict]:
    """Running machines of the same type that could take the work, most free capacity first."""
    out = []
    for m in machines:
        if m["machine_id"] == machine["machine_id"] or m.get("type") != machine.get("type") or m["machine_id"] in unavailable:
            continue
        spare = spare_hours(open_orders(book, m["machine_id"]), rates.get(m["machine_id"], 1), now)
        out.append({"machine_id": m["machine_id"], "name": m.get("name"), "line": m.get("line"), "spare_hours_7d": spare,
                    "covers": spare >= need_h})
    return sorted(out, key=lambda x: -x["spare_hours_7d"])


def work_hours(orders: list[dict], rate_h: float) -> float:
    return round(sum(float(o["qty"]) for o in orders) / max(float(rate_h or 0), 0.1), 1)
