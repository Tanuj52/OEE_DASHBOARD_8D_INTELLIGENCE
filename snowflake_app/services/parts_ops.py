"""
Spare parts and purchasing: the stock ledger, work-order reservations and the
purchase-order flow (requested -> approved -> ordered -> received).

PRODUCTION.PARTS_INVENTORY is the opening balance and PART_VENDORS the vendor
master; every movement since is a row in OPERATIONS.PART_TRANSACTIONS
(core/parts.py does the arithmetic). Work orders reserve their parts when they
are raised, issue what the technician used when the repair is completed and
release the rest; a cancelled order releases everything. Ledger ids are derived
from the work order or purchase order, so repeating a step never double-counts.
"""

import time
import uuid
from datetime import datetime, timedelta

from snowflake_app.core import data as D, maintenance as MX, parts as P
from snowflake_app.services import ops_store

TXN, PO = "part_transactions", "purchase_orders"


def store():
    return ops_store.store()


def master() -> list[dict]:
    """Parts with their vendor, lead time and minimum order quantity."""
    parts = D.records(D.table("parts_inventory"))
    vend = {v["part_id"]: v for v in D.records(D.table("part_vendors"))}
    out = []
    for p in parts:
        v = vend.get(p["part_id"]) or {}
        out.append({**p, "vendor_name": v.get("vendor_name"), "lead_time_days": int(v.get("lead_time_days") or 7),
                    "min_order_qty": int(v.get("min_order_qty") or 1), "vendor_cost_usd": v.get("unit_cost_usd") or p["unit_cost_usd"],
                    "vendor_email": v.get("vendor_email")})
    return out


def by_id() -> dict[str, dict]:
    return {p["part_id"]: p for p in master()}


def txns() -> list[dict]:
    return sorted(store().all(TXN), key=lambda t: str(t.get("ts") or ""))


def pos() -> list[dict]:
    return sorted(store().all(PO), key=lambda p: str(p.get("created_at") or ""), reverse=True)


def stock() -> dict[str, dict]:
    return P.stock(master(), txns())


def _txn(txn_id: str, part_id: str, kind: str, qty: int, *, wo_id=None, po_id=None, user=None, note="") -> bool:
    st = store()
    if st.get(TXN, txn_id):
        return False
    st.put(TXN, {"txn_id": txn_id, "ts": ops_store.now_iso(), "part_id": part_id, "kind": kind, "qty": int(qty),
                 "wo_id": wo_id, "po_id": po_id, "user_name": user, "note": note})
    return True


# ------------------------------------------------------------------ work orders
def reserve_for_wo(wo: dict) -> int:
    """Hold one of each part the work order lists (parts_for: compatible with the machine and the failure)."""
    return sum(_txn(f"R-{wo['wo_id']}-{p['part_id']}", p["part_id"], "reserve", 1, wo_id=wo["wo_id"],
                    note=f"Reserved for {wo['wo_id']} ({wo['machine_id']})") for p in wo.get("parts") or [])


def held(wo_id: str) -> dict[str, int]:
    return P.reservations(txns()).get(wo_id, {})


def release_for_wo(wo_id: str, note: str = "") -> int:
    return sum(_txn(f"L-{wo_id}-{pid}", pid, "release", q, wo_id=wo_id, note=note or f"Released from {wo_id}")
               for pid, q in held(wo_id).items())


def complete_for_wo(wo_id: str, used: list[dict] | None, user: str | None = None) -> list[dict]:
    """Issue what was used ({part_id, qty}; None = what was reserved) and release the rest."""
    hold = held(wo_id)
    if used is None:
        used = [{"part_id": pid, "qty": q} for pid, q in hold.items()]
    release_for_wo(wo_id, f"Repair {wo_id} completed")
    names = by_id()
    out = []
    for u in used:
        q = int(u.get("qty") or 0)
        if q > 0 and u.get("part_id") in names:
            _txn(f"I-{wo_id}-{u['part_id']}", u["part_id"], "issue", q, wo_id=wo_id, user=user, note=f"Used in {wo_id}")
            out.append({"part_id": u["part_id"], "part_name": names[u["part_id"]]["part_name"], "qty": q})
    return out


def wo_parts(wo: dict, stk: dict | None = None, cov: dict | None = None, inc: dict | None = None) -> list[dict]:
    """Live status of each part a work order needs: held and covered by the shelf, or short (with lead time)."""
    stk = stk if stk is not None else stock()
    t = txns()
    cov = cov if cov is not None else P.coverage(t, stk)
    inc = inc if inc is not None else P.incoming(pos())
    hold = P.reservations(t).get(wo["wo_id"], {})
    info = by_id()
    out = []
    for p in wo.get("parts") or []:
        pid = p["part_id"]
        s, m = stk.get(pid) or {}, info.get(pid) or {}
        reserved = hold.get(pid, 0)
        covered = cov.get((wo["wo_id"], pid), s.get("available", 0) > 0) if reserved else s.get("available", 0) > 0
        out.append({"part_id": pid, "part_name": p["part_name"], "reserved": reserved, "covered": bool(covered),
                    "on_hand": s.get("on_hand", 0), "available": s.get("available", 0),
                    "lead_time_days": m.get("lead_time_days"), "vendor_name": m.get("vendor_name"),
                    "incoming": (inc.get(pid) or {}).get("qty", 0), "eta": (inc.get(pid) or {}).get("first_eta")})
    return out


def need_by(wo: dict, live_rul: float | None = None, now: float | None = None) -> float | None:
    """When a job needs its parts: now for a stopped machine, before the forecast failure for a planned job."""
    now = now or time.time()
    if wo["status"] in ("awaiting", "in_repair"):
        return now
    if wo["status"] == "planned":
        if live_rul is not None:
            return now + live_rul * 3600
        rul = wo.get("rul_hours_at_open")
        try:
            created = datetime.fromisoformat(wo["created_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            created = now
        return created + float(rul) * 3600 if rul is not None else None
    return None


def short_jobs(open_wos: list[dict], outlook: dict, stk: dict, t: list[dict]) -> dict[str, list[dict]]:
    cov = P.coverage(t, stk)
    out: dict[str, list[dict]] = {}
    for (wo_id, pid), ok in cov.items():
        if ok:
            continue
        wo = next((w for w in open_wos if w["wo_id"] == wo_id), None)
        if wo:
            rul = (outlook.get(wo["machine_id"]) or {}).get("rul_hours")
            out.setdefault(pid, []).append({"wo_id": wo_id, "machine_id": wo["machine_id"], "need_by": need_by(wo, rul)})
    return out


def plan(outlook: dict, open_wos: list[dict], now: float | None = None) -> dict:
    """Everything the Spare parts page and the digest need in one pass."""
    now = now or time.time()
    mst = master()
    t = txns()
    stk = P.stock(mst, t)
    inc = P.incoming(pos())
    lead = {p["part_id"]: p["lead_time_days"] for p in mst}
    fc = P.forecast_demand(list(outlook.values()), MX.parts_for, lead)
    shorts = short_jobs(open_wos, outlook, stk, t)
    sugg = P.suggestions(mst, stk, inc, fc, shorts, now)
    return {"master": mst, "stock": stk, "incoming": inc, "forecast": fc, "shorts": shorts, "suggestions": sugg, "txns": t,
            "value": round(sum(max(s["on_hand"], 0) * float(p["unit_cost_usd"]) for p in mst for s in [stk[p["part_id"]]]), 2)}


# ------------------------------------------------------------------ purchasing
def create_po(part_id: str, qty: int, reason: str = "", wo_id: str | None = None, user: str | None = None) -> dict:
    p = by_id()[part_id]
    po = {"po_id": f"PO-{datetime.now():%y%m%d}-{uuid.uuid4().hex[:4].upper()}", "part_id": part_id, "part_name": p["part_name"],
          "vendor_name": p["vendor_name"], "vendor_email": p.get("vendor_email"), "qty": max(int(qty), 1),
          "unit_cost_usd": float(p["vendor_cost_usd"]), "status": "requested", "reason": reason, "wo_id": wo_id,
          "lead_time_days": p["lead_time_days"], "created_at": ops_store.now_iso(), "ordered_at": None, "expected_at": None,
          "received_at": None, "created_by": user,
          "history": [{"at": ops_store.now_iso(), "by": user or "Plant user", "what": f"Requested {int(qty)} × {p['part_name']}"}]}
    store().put(PO, po)
    return po


NEXT = {"approve": ("requested", "approved"), "order": ("approved", "ordered"), "receive": ("ordered", "received"),
        "cancel": (None, "cancelled")}


def po_action(po_id: str, action: str, user: str | None = None) -> dict | None:
    st = store()
    po = st.get(PO, po_id)
    if not po or action not in NEXT:
        return None
    need, to = NEXT[action]
    if (need and po["status"] != need) or (action == "cancel" and po["status"] in ("received", "cancelled")):
        return None
    now = ops_store.now_iso()
    po["status"] = to
    if action == "order":
        po["ordered_at"] = now
        po["expected_at"] = (datetime.now().astimezone() + timedelta(days=int(po.get("lead_time_days") or 7))).isoformat(timespec="seconds")
    elif action == "receive":
        po["received_at"] = now
        _txn(f"V-{po_id}", po["part_id"], "receive", po["qty"], po_id=po_id, user=user, note=f"Received {po_id} from {po['vendor_name']}")
    po["history"] = (po.get("history") or []) + [{"at": now, "by": user or "Plant user", "what": to.capitalize()}]
    st.put(PO, po)
    return po


def adjust(part_id: str, qty: int, note: str, user: str | None = None) -> bool:
    if not qty:
        return False
    return _txn(f"A-{part_id}-{uuid.uuid4().hex[:6]}", part_id, "adjust", int(qty), user=user, note=note or "Stock count")


def ensure_reservations(open_wos: list[dict]) -> int:
    """Work orders raised before the ledger existed get their reservations once."""
    have = {t.get("wo_id") for t in store().all(TXN) if t.get("kind") == "reserve"}
    return sum(reserve_for_wo(w) for w in open_wos if w["wo_id"] not in have and w.get("parts"))
