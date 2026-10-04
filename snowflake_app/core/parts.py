"""
Spare parts: stock from the opening balance plus the ledger, what is held for
work orders, what machines heading for failure will need, and what to order.

Ledger kinds (OPERATIONS.PART_TRANSACTIONS):
  reserve / release  hold a part for a work order, or let it go again
  issue              the part left the store (used in a repair)
  receive            a purchase order was delivered
  adjust             stock count correction (+/-)

on_hand   = opening + received - issued + adjusted
reserved  = reserved - released
available = on_hand - reserved   (negative: more is promised than is on the shelf)

Pure functions over plain dicts; services/parts_ops.py feeds them from the
operations store and the plant tables.
"""

import math
from datetime import datetime

INCOMING = ("requested", "approved", "ordered")
PO_FLOW = ("requested", "approved", "ordered", "received")
FORECAST_BUFFER_DAYS = 7  # look this far past a part's lead time for machines heading for failure


def stock(parts: list[dict], txns: list[dict]) -> dict[str, dict]:
    out = {p["part_id"]: {"opening": int(p.get("qty_on_hand") or 0), "received": 0, "issued": 0, "adjusted": 0,
                          "reserved": 0} for p in parts}
    for t in txns:
        s = out.get(t.get("part_id"))
        if s is None:
            continue
        q, k = int(t.get("qty") or 0), t.get("kind")
        if k == "receive":
            s["received"] += q
        elif k == "issue":
            s["issued"] += q
        elif k == "adjust":
            s["adjusted"] += q
        elif k == "reserve":
            s["reserved"] += q
        elif k == "release":
            s["reserved"] -= q
    for s in out.values():
        s["reserved"] = max(s["reserved"], 0)
        s["on_hand"] = s["opening"] + s["received"] - s["issued"] + s["adjusted"]
        s["available"] = s["on_hand"] - s["reserved"]
    return out


def reservations(txns: list[dict]) -> dict[str, dict[str, int]]:
    """{wo_id: {part_id: qty still held}}."""
    out: dict[str, dict[str, int]] = {}
    for t in txns:
        if t.get("kind") in ("reserve", "release") and t.get("wo_id"):
            d = out.setdefault(t["wo_id"], {})
            d[t["part_id"]] = d.get(t["part_id"], 0) + int(t.get("qty") or 0) * (1 if t["kind"] == "reserve" else -1)
    return {w: {p: q for p, q in d.items() if q > 0} for w, d in out.items() if any(q > 0 for q in d.values())}


def coverage(txns: list[dict], stk: dict[str, dict]) -> dict[tuple[str, str], bool]:
    """(wo_id, part_id) -> whether the shelf covers that reservation; the earliest reservations are served first."""
    held = reservations(txns)
    first = {}
    for t in sorted(txns, key=lambda t: str(t.get("ts") or "")):
        if t.get("kind") == "reserve" and t.get("wo_id") in held:
            first.setdefault((t["wo_id"], t["part_id"]), str(t.get("ts") or ""))
    used: dict[str, int] = {}
    out = {}
    for (wo, pid), _ in sorted(first.items(), key=lambda kv: kv[1]):
        q = held.get(wo, {}).get(pid, 0)
        if not q:
            continue
        used[pid] = used.get(pid, 0) + q
        out[(wo, pid)] = used[pid] <= (stk.get(pid) or {}).get("on_hand", 0)
    return out


def incoming(pos: list[dict]) -> dict[str, dict]:
    """{part_id: {qty, ordered, first_eta}} for purchase orders not yet received."""
    out: dict[str, dict] = {}
    for po in pos:
        if po.get("status") not in INCOMING:
            continue
        d = out.setdefault(po["part_id"], {"qty": 0, "ordered": 0, "first_eta": None, "pos": []})
        d["qty"] += int(po.get("qty") or 0)
        d["pos"].append(po["po_id"])
        if po["status"] == "ordered":
            d["ordered"] += int(po.get("qty") or 0)
            eta = po.get("expected_at")
            if eta and (d["first_eta"] is None or str(eta) < str(d["first_eta"])):
                d["first_eta"] = eta
    return out


def forecast_demand(outlook: list[dict], parts_for, lead_days: dict[str, int]) -> dict[str, list[dict]]:
    """Parts that machines heading for failure will need before a new order could arrive.

    outlook: [{machine_id, rul_hours, fault, has_open_wo}], parts_for(machine_id, fault) -> [part dicts].
    A machine counts for a part when it has no work order yet (an open order already reserves its parts)
    and its time to failure is within the part's lead time plus FORECAST_BUFFER_DAYS."""
    out: dict[str, list[dict]] = {}
    for m in outlook:
        h = m.get("rul_hours")
        if h is None or m.get("has_open_wo"):
            continue
        for p in parts_for(m["machine_id"], m.get("fault")):
            if h <= (lead_days.get(p["part_id"], 7) + FORECAST_BUFFER_DAYS) * 24:
                out.setdefault(p["part_id"], []).append({"machine_id": m["machine_id"], "rul_hours": h})
    return out


def suggestions(master: list[dict], stk: dict, inc: dict, forecast: dict, short_wos: dict[str, list[dict]],
                now: float) -> list[dict]:
    """What to order: parts whose projected stock (available + incoming - forecast need) is at or below the
    reorder point, ordered up to twice the reorder point (at least the vendor's minimum order).

    short_wos: {part_id: [{wo_id, machine_id, need_by (epoch or None)}]} work orders the shelf cannot cover;
    a part is flagged too_late when its lead time ends after one of those jobs needs it."""
    out = []
    for p in master:
        pid = p["part_id"]
        s = stk.get(pid) or {"on_hand": 0, "available": 0, "reserved": 0}
        need = len(forecast.get(pid, []))
        in_q = (inc.get(pid) or {}).get("qty", 0)
        projected = s["available"] + in_q - need
        rop = int(p.get("reorder_point") or 0)
        shorts = short_wos.get(pid, [])
        if projected > rop and not shorts:
            continue
        lead = int(p.get("lead_time_days") or 7)
        arrives = now + lead * 86400
        late_for = [w for w in shorts if w.get("need_by") is not None and w["need_by"] < arrives]
        qty = max(int(p.get("min_order_qty") or 1), 2 * rop - projected, 1)
        if s["available"] < 0 or s["on_hand"] <= 0:
            urgency = "stock_out"
        elif need and projected <= rop:
            urgency = "forecast"
        else:
            urgency = "below_reorder"
        out.append({"part_id": pid, "part_name": p.get("part_name"), "vendor_name": p.get("vendor_name"),
                    "qty": int(math.ceil(qty)), "unit_cost_usd": float(p.get("vendor_cost_usd") or p.get("unit_cost_usd") or 0),
                    "lead_time_days": lead, "arrives_at": datetime.fromtimestamp(arrives).astimezone().isoformat(timespec="seconds"),
                    "projected": projected, "reorder_point": rop, "forecast_need": need, "incoming": in_q,
                    "urgency": urgency, "late_for": late_for, "machines": [f["machine_id"] for f in forecast.get(pid, [])]})
    order = {"stock_out": 0, "forecast": 1, "below_reorder": 2}
    return sorted(out, key=lambda r: (order[r["urgency"]], -len(r["late_for"]), r["part_id"]))
