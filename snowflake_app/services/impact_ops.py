"""
Business impact for the app: the order book (PRODUCTION.ORDER_BOOK), machine
capacity and the live plant state put through core/impact.py.

  down machine        the current stop: from when it stopped until the repair is
                      expected to finish (awaiting: the estimate; in repair: the
                      estimate minus the time already spent)
  heading for failure if it fails when forecast (unplanned repair time and cost),
                      and for a planned job the best window before then
Cost settings (margin, labour rate, crew) are plant-wide: Settings > Business.
"""

import time
from datetime import datetime

from snowflake_app.core import data as D, impact as I, maintenance as MX

_est_cache: dict = {}


def cfg(settings: dict | None) -> dict:
    return {**I.DEFAULTS, **((settings or {}).get("business") or {})}


def _epoch(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def estimate(mid: str, kind: str) -> dict:
    key = (id(D.table("maintenance_logs")), mid, kind)
    if key not in _est_cache:
        if len(_est_cache) > 200:
            _est_cache.clear()
        _est_cache[key] = MX.estimate(mid, kind)
    return _est_cache[key]


def context() -> dict:
    return {"book": D.order_book(), "rates": D.machine_rates(), "machines": D.records(D.table("machines")),
            "customers": D.customer_names()}


def remaining_hours(mid: str, wo: dict | None, now: float) -> float:
    """Hours until a stopped machine is expected to run again."""
    if not wo:
        return estimate(mid, "unplanned")["hours"]
    est = float(wo.get("est_hours") or estimate(mid, wo.get("kind") or "unplanned")["hours"])
    if wo["status"] == "in_repair":
        started = _epoch(wo.get("started_at")) or now
        return max(est - (now - started) / 3600, 0.25)
    return est if wo["status"] == "awaiting" else 0.0


def machine(m: dict, row: dict | None, wo: dict | None, conf: dict, ctx: dict, now: float, horizon_h: float = 72) -> dict:
    mid = m["machine_id"]
    orders = I.open_orders(ctx["book"], mid)
    rate = ctx["rates"].get(mid, 1.0)
    out = {"machine_id": mid, "name": m.get("name"), "line": m.get("line"), "type": m.get("type"), "orders": orders,
           "open_orders": len(orders), "order_value": round(sum(float(o["order_value_usd"]) for o in orders), 2),
           "rate_h": rate, "cost_per_h": I.cost_per_hour(orders, rate, conf), "status": (row or {}).get("status"),
           "lifecycle": (row or {}).get("lifecycle"), "rul_hours": (row or {}).get("rul_hours"), "work_order": wo,
           "scenario": None, "window": None, "alternates": []}
    down = (row or {}).get("status") == "down" or (wo and wo["status"] in ("awaiting", "in_repair"))
    if down:
        start = _epoch((row or {}).get("down_since")) or _epoch((wo or {}).get("down_at")) or now
        rem = remaining_hours(mid, wo, now)
        hours = max((now - start) / 3600, 0) + rem
        sc = I.stop_cost(orders, rate, start, hours, float((wo or {}).get("est_cost_usd") or 0), conf, ctx["customers"])
        out["scenario"] = {"kind": "down", "label": "Current stop", "remaining_h": round(rem, 2), "since": start, **sc}
    elif out["rul_hours"] is not None and out["rul_hours"] <= horizon_h:
        rul = max(float(out["rul_hours"]), 0)
        est = estimate(mid, "unplanned")
        sc = I.stop_cost(orders, rate, now + rul * 3600, est["hours"], est["cost_usd"], conf, ctx["customers"])
        out["scenario"] = {"kind": "forecast", "label": "If it fails as forecast", "in_h": rul, **sc}
        if wo and wo["status"] == "planned":
            job_h = float(wo.get("est_hours") or estimate(mid, "planned")["hours"])
            bw = I.best_window(orders, rate, now, job_h, rul, conf)
            planned = I.stop_cost(orders, rate, bw["start_at"], job_h, float(wo.get("est_cost_usd") or 0), conf, ctx["customers"])
            out["window"] = {**bw, "hours": job_h, "cost": planned["total_cost"],
                             "saves": round(sc["total_cost"] - planned["total_cost"], 2)}
    sc = out["scenario"]
    if sc and sc["late"]:
        need = I.work_hours(sc["late"], rate)
        out["alternates"] = I.alternates(m, ctx["machines"], ctx["book"], ctx["rates"], now, need,
                                         ctx.get("unavailable", set()))[:3]
    return out


def plant(outlook: dict, work_orders: list[dict], settings: dict | None, now: float | None = None) -> dict:
    """Impact for every machine plus the plant totals shown on the Business impact page."""
    now = now or time.time()
    conf = cfg(settings)
    horizon = float(((settings or {}).get("automation") or {}).get("plan_hours", 72))
    ctx = context()
    open_wo = {}
    for w in work_orders:
        if w["status"] in ("planned", "awaiting", "in_repair", "commissioning"):
            open_wo.setdefault(w["machine_id"], w)
    ctx["unavailable"] = {mid for mid, r in outlook.items() if r.get("status") in ("down", "critical")} | {
        mid for mid, w in open_wo.items() if w["status"] in ("awaiting", "in_repair")}
    rows = [machine(m, outlook.get(m["machine_id"]), open_wo.get(m["machine_id"]), conf, ctx, now, horizon)
            for m in ctx["machines"]]
    by = {r["machine_id"]: r for r in rows}
    cost_7d = 0.0
    for w in work_orders:
        r = by.get(w["machine_id"])
        if not r:
            continue
        if w["status"] == "closed" and w.get("downtime_hours") and (_epoch(w.get("closed_at")) or 0) > now - 7 * 86400:
            cost_7d += float(w["downtime_hours"]) * r["cost_per_h"]["total_h"]
        elif w["status"] in ("awaiting", "in_repair") and _epoch(w.get("down_at")):
            cost_7d += max(now - max(_epoch(w["down_at"]), now - 7 * 86400), 0) / 3600 * r["cost_per_h"]["total_h"]
    now_sc = [r for r in rows if r["scenario"] and r["scenario"]["kind"] == "down"]
    fc_sc = [r for r in rows if r["scenario"] and r["scenario"]["kind"] == "forecast"]
    late_now = [o for r in now_sc for o in r["scenario"]["late"]]
    late_fc = [o for r in fc_sc for o in r["scenario"]["late"]]
    hist = D.oee_history()
    judged = [h for h in hist if h["oee"] is not None]
    units = sum(h["units"] for h in judged) or 1
    return {"machines": rows, "by_id": by, "down": now_sc, "forecast": fc_sc, "conf": conf,
            "kpis": {"oee": round(sum(h["oee"] * h["units"] for h in judged) / units, 4) if judged else None,
                     "downtime_cost_7d": round(cost_7d, 2),
                     "late_now": len(late_now), "value_now": round(sum(float(o["order_value_usd"]) for o in late_now), 2),
                     "penalty_now": round(sum(o["penalty_usd"] for o in late_now), 2),
                     "late_forecast": len(late_fc), "value_forecast": round(sum(float(o["order_value_usd"]) for o in late_fc), 2),
                     "customers": sorted({o["customer_name"] for o in late_now + late_fc if o.get("customer_name")}),
                     "open_orders": sum(r["open_orders"] for r in rows),
                     "open_value": round(sum(r["order_value"] for r in rows), 2)},
            "oee": hist}


def for_machine(mid: str, outlook: dict, work_orders: list[dict], settings: dict | None, now: float | None = None) -> dict | None:
    """One machine's impact (work-order cards, live monitoring, emails)."""
    now = now or time.time()
    ctx = context()
    m = next((x for x in ctx["machines"] if x["machine_id"] == mid), None)
    if not m:
        return None
    wo = next((w for w in work_orders if w["machine_id"] == mid and w["status"] in ("planned", "awaiting", "in_repair")), None)
    ctx["unavailable"] = {k for k, r in outlook.items() if r.get("status") in ("down", "critical")}
    horizon = float(((settings or {}).get("automation") or {}).get("plan_hours", 72))
    return machine(m, outlook.get(mid), wo, cfg(settings), ctx, now, horizon)
