"""
Maintenance planning from the plant tables: work orders for a machine that is
degrading (planned) or has stopped (unplanned).

Estimates come from the machine's own maintenance history in MAINTENANCE_LOGS
(median duration and cost of Scheduled vs Unscheduled work), falling back to
the plant-wide medians; spare parts from PARTS_INVENTORY filtered by machine
compatibility and the failing component; the technician from OPERATORS
certifications. Framework-free: used by the Streamlit runner.
"""

import re
import uuid
from datetime import datetime

from snowflake_app.core import data as D

# failure mode -> (component wording, part keywords)
FAULT_PARTS = {
    "bearing_wear": ("Spindle bearing", ["bearing"]),
    "thermal_runaway": ("Spindle cooling and temperature control", ["coolant", "thermocouple", "seal"]),
    "hydraulic_leak": ("Hydraulic seals and lines", ["hydraulic", "seal"]),
    "motor_overload": ("Drive motor and belt train", ["motor", "belt"]),
    "coolant_blockage": ("Coolant pump and filter", ["coolant", "filter", "seal"]),
    "belt_slip": ("Spindle drive belt and pulleys", ["belt"]),
}
CHECKLIST = {
    "bearing_wear": ["Lock out and tag out the machine", "Inspect spindle bearings for play, noise and heat marks",
                     "Replace the bearing set and check spindle run-out", "Run a warm-up cycle and verify vibration is back to normal"],
    "thermal_runaway": ["Lock out and tag out the machine", "Check coolant level, flow and the chiller",
                        "Inspect seals and the temperature sensor", "Run a warm-up cycle and verify temperature stays in range"],
    "hydraulic_leak": ["Lock out and tag out the machine, release hydraulic pressure", "Find the leak at seals, fittings and hoses",
                       "Replace the seal kit and top up hydraulic oil", "Pressure-test and verify pressure holds"],
    "motor_overload": ["Lock out and tag out the machine", "Check motor current, belt tension and mechanical binding",
                       "Replace worn belts or motor components", "Run under load and verify power draw is normal"],
    "coolant_blockage": ["Lock out and tag out the machine", "Flush the coolant line and clean the filter",
                         "Inspect the coolant pump and seals", "Verify coolant flow is back in range"],
    "belt_slip": ["Lock out and tag out the machine", "Check drive belt tension, glazing and pulley alignment",
                  "Replace the V-belt set and re-tension", "Run under load and verify spindle speed holds at setpoint"],
}
PRIORITY = {"P1": "Emergency", "P2": "Urgent", "P3": "Planned"}


def _median(series, default):
    try:
        v = float(series.median())
        return v if v == v else default
    except Exception:
        return default


def estimate(machine_id: str, kind: str) -> dict:
    """Expected duration and cost for planned (Scheduled) or unplanned (Unscheduled) work."""
    logs = D.table("maintenance_logs")
    want = "Scheduled" if kind == "planned" else "Unscheduled"
    plant = logs[logs["type"] == want]
    mine = plant[plant["machine_id"] == machine_id]
    basis = mine if len(mine) >= 2 else plant
    hours = round(_median(basis["duration_hrs"], 1.6 if kind == "planned" else 5.7), 1)
    cost = round(_median(basis["cost_usd"], 232 if kind == "planned" else 2840))
    alt = logs[logs["type"] == ("Unscheduled" if kind == "planned" else "Scheduled")]
    return {"hours": hours, "cost_usd": cost, "samples": len(basis),
            "basis": f"{machine_id} history ({len(mine)} jobs)" if basis is mine else f"plant median ({len(plant)} jobs)",
            "plant_unplanned_cost": round(_median(logs[logs["type"] == "Unscheduled"]["cost_usd"], 2840)),
            "plant_planned_cost": round(_median(logs[logs["type"] == "Scheduled"]["cost_usd"], 232)),
            "plant_unplanned_hours": round(_median(logs[logs["type"] == "Unscheduled"]["duration_hrs"], 5.7), 1),
            "plant_planned_hours": round(_median(logs[logs["type"] == "Scheduled"]["duration_hrs"], 1.6), 1),
            "alt_samples": len(alt)}


def parts_for(machine_id: str, fault: str | None) -> list[dict]:
    parts = D.table("parts_inventory")
    compat = parts[parts["machine_compatibility"].fillna("").str.contains(machine_id)]
    words = FAULT_PARTS.get(fault or "", ("", []))[1]
    out = []
    for p in D.records(compat):
        text = f"{p['part_name']} {p['category']}".lower()
        if words and not any(w in text for w in words):
            continue
        qty, rop = int(p["qty_on_hand"]), int(p["reorder_point"])
        out.append({"part_id": p["part_id"], "part_name": p["part_name"], "qty_on_hand": qty, "reorder_point": rop,
                    "unit_cost_usd": float(p["unit_cost_usd"]), "in_stock": qty > 0, "below_reorder": qty <= rop})
    return out


def _level(cert: str) -> int:
    m = re.search(r"Level (\d)", cert)
    return int(m.group(1)) if m else 0


def technician(machine_type: str) -> dict | None:
    ops = D.records(D.table("operators"))
    best, score = None, -1
    for o in ops:
        certs = [c.strip() for c in str(o.get("certifications") or "").split(",")]
        for c in certs:
            s = -1
            if c.startswith("Maintenance"):
                s = 10 + _level(c)
            elif machine_type and c.startswith(machine_type) and _level(c) >= 2:
                s = _level(c)
            if s > score:
                best, score = o, s
    if not best:
        return None
    return {"id": best["operator_id"], "name": best["name"], "shift": best.get("shift"), "certifications": best.get("certifications")}


def technicians(machine_type: str) -> list[dict]:
    """Everyone who can work on this machine type, best match first."""
    out = []
    for o in D.records(D.table("operators")):
        certs = [c.strip() for c in str(o.get("certifications") or "").split(",")]
        s = max([10 + _level(c) if c.startswith("Maintenance") else (_level(c) if machine_type and c.startswith(machine_type) else -1)
                 for c in certs] or [-1])
        if s >= 2:
            out.append((s, {"id": o["operator_id"], "name": o["name"], "shift": o.get("shift"), "certifications": o.get("certifications")}))
    return [t for _, t in sorted(out, key=lambda x: -x[0])]


def build_work_order(machine: dict, kind: str, *, fault: str | None, sensor: str | None, reason: str,
                     rul_hours: float | None, now_iso: str, down_at: str | None = None) -> dict:
    """machine: {machine_id, name, line, type}. kind: planned | unplanned."""
    mid = machine["machine_id"]
    est = estimate(mid, kind)
    tech = technician(machine.get("type") or "")
    component = FAULT_PARTS.get(fault or "", ("General inspection", []))[0]
    if kind == "unplanned":
        prio = "P1"
    elif rul_hours is not None and rul_hours < 24:
        prio = "P2"
    else:
        prio = "P3"
    parts = parts_for(mid, fault)
    return {
        "wo_id": f"WO-{datetime.now():%y%m%d}-{uuid.uuid4().hex[:4].upper()}",
        "machine_id": mid, "machine_name": machine.get("name"), "line": machine.get("line"), "machine_type": machine.get("type"),
        "kind": kind, "priority": prio, "priority_label": PRIORITY[prio],
        "status": "awaiting" if down_at else "planned",
        "failure_mode": fault, "component": component, "sensor": sensor, "reason": reason,
        "created_at": now_iso, "down_at": down_at, "started_at": None, "completed_at": None, "closed_at": None,
        "rul_hours_at_open": rul_hours, "est_hours": est["hours"], "est_cost_usd": est["cost_usd"], "estimate_basis": est["basis"],
        "avoided_cost_usd": max(est["plant_unplanned_cost"] - est["plant_planned_cost"], 0) if kind == "planned" else 0,
        "avoided_hours": max(est["plant_unplanned_hours"] - est["plant_planned_hours"], 0) if kind == "planned" else 0,
        "parts": parts, "parts_ready": all(p["in_stock"] for p in parts) if parts else None,
        "technician_id": tech["id"] if tech else None, "technician_name": tech["name"] if tech else None,
        "checklist": CHECKLIST.get(fault or "", ["Lock out and tag out the machine", "Inspect the machine", "Repair", "Verify normal operation"]),
        "downtime_hours": None, "notes": "", "parts_used": [], "run_id": None,
    }
