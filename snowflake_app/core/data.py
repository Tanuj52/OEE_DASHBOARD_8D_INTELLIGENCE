"""
Cached access to the manufacturing dataset plus the aggregations used by
the Command Center, the 8D agents' tools and the quality pages.

Shared by the Streamlit app and the React/FastAPI backend.

Two sources, same tables and shapes:
  files     - CSVs in data/synthetic (or MFG_DATA_DIR); the default.
  snowflake - tables in a Snowflake schema (default MANUFACTURING_8D.PRODUCTION,
              loaded by sql/setup.py). The caller supplies a query function
              (the Streamlit app passes its session); or set
              MFG_DATA_SOURCE=snowflake and MFG_SNOWFLAKE_CONNECTION=<name>.
Each table is read once and cached (the rolling ORDER_BOOK is re-read every
REFRESH_S, as a Snowflake task rolls it daily); use_files()/use_snowflake()
clear the caches. With files, the order book is rolled from the order history
by core/orderbook.py, the same code the Snowflake task runs.
"""

import os
import re
import threading
import time
from datetime import date
from functools import lru_cache
from typing import Callable

import numpy as np
import pandas as pd

from snowflake_app.core import orderbook as OB
from snowflake_app.tools.prediction_engine import scan_all_machines, estimate_rul, SENSOR_THRESHOLDS

DATA_DIR = os.environ.get("MFG_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "synthetic")
DEFAULT_SCHEMA = "MANUFACTURING_8D.PRODUCTION"
TABLES = ["machines", "operators", "suppliers", "customers", "material_lots", "production_orders", "bom_consumption",
          "sensor_readings", "process_records", "quality_inspections", "defect_records", "maintenance_logs",
          "shipments", "parts_inventory", "products", "part_vendors", "order_book"]
REFRESH_S = {"order_book": 3600}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")
from snowflake_app.tools.spc_analysis import run_spc_analysis
from snowflake_app.tools.lot_traceability import trace_full
from snowflake_app.tools.copq_calculator import calculate_copq

SENSORS = list(SENSOR_THRESHOLDS.keys())
SEVERITY_ORDER = {"Critical": 0, "Major": 1, "Minor": 2}


class DataSourceError(RuntimeError):
    pass


_source: dict = {"kind": "files"}
_cache: dict[str, pd.DataFrame] = {}
_loaded: dict[str, float] = {}
_lock = threading.RLock()


def _clear():
    _cache.clear()
    _loaded.clear()
    historical_alerts.cache_clear()
    defects_enriched.cache_clear()
    machine_rates.cache_clear()
    oee_history.cache_clear()


def use_files():
    with _lock:
        if _source.get("kind") != "files":
            _source.clear()
            _source["kind"] = "files"
            _clear()


def use_snowflake(query_df: Callable[[str], pd.DataFrame], schema: str = DEFAULT_SCHEMA, key: str = ""):
    """Read tables from `schema` with query_df(sql) -> DataFrame. `key` identifies the session/config
    so switching connections clears the caches."""
    schema = (schema or DEFAULT_SCHEMA).strip()
    if not _IDENT.match(schema):
        raise DataSourceError(f"Schema must be DATABASE.SCHEMA, got {schema!r}")
    with _lock:
        if _source.get("kind") == "snowflake" and _source.get("schema") == schema.upper() and _source.get("key") == key:
            _source["query"] = query_df
            return
        _source.clear()
        _source.update(kind="snowflake", schema=schema.upper(), query=query_df, key=key)
        _clear()


def source_info() -> dict:
    return {"kind": _source["kind"], "schema": _source.get("schema"), "loaded": sorted(_cache)}


def _env_source():
    """MFG_DATA_SOURCE=snowflake with MFG_SNOWFLAKE_CONNECTION configures the source for non-Streamlit callers."""
    if _source["kind"] == "files" and os.environ.get("MFG_DATA_SOURCE", "").lower() == "snowflake" and not _source.get("env_done"):
        _source["env_done"] = True
        import snowflake.connector

        conn = snowflake.connector.connect(connection_name=os.environ.get("MFG_SNOWFLAKE_CONNECTION") or None)

        def q(sql):
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetch_pandas_all()
        use_snowflake(q, os.environ.get("MFG_SNOWFLAKE_SCHEMA", DEFAULT_SCHEMA), key="env")


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Snowflake rows -> the CSV shapes: lower-case columns, ISO date/timestamp strings, plain numerics."""
    df.columns = [c.lower() for c in df.columns]
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            df[c] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
        elif s.dtype == object:
            first = s.dropna().head(1)
            if len(first) and hasattr(first.iloc[0], "isoformat") and not isinstance(first.iloc[0], str):
                fmt = "%Y-%m-%d %H:%M:%S" if hasattr(first.iloc[0], "hour") else "%Y-%m-%d"
                df[c] = s.map(lambda v: v.strftime(fmt) if v is not None and not pd.isna(v) else None)
            elif len(first) and hasattr(first.iloc[0], "as_integer_ratio") and not isinstance(first.iloc[0], (int, float)):
                df[c] = pd.to_numeric(s)  # Decimal -> float
    return df


def table(name: str) -> pd.DataFrame:
    with _lock:
        _env_source()
        if name in _cache and time.time() - _loaded.get(name, 0) < REFRESH_S.get(name, float("inf")):
            return _cache[name]
        if _source["kind"] == "snowflake":
            if name not in TABLES:
                raise DataSourceError(f"Unknown table {name}")
            try:
                df = _normalise(_source["query"](f"SELECT * FROM {_source['schema']}.{name.upper()}"))
            except Exception as e:
                raise DataSourceError(f"Could not read {_source['schema']}.{name.upper()}: {str(e).splitlines()[0][:300]}") from e
        else:
            path = os.path.join(DATA_DIR, f"{name}.csv")
            if os.path.exists(path):
                df = pd.read_csv(path)
            elif name == "order_book":
                df = _rolled_order_book()
            else:
                df = pd.DataFrame()
        _cache[name] = df
        _loaded[name] = time.time()
        return df


def _rolled_order_book() -> pd.DataFrame:
    """Files only: today's order book rolled from the order history (Snowflake keeps a stored one)."""
    res = OB.roll(date.today(), records(table("machines")), records(table("production_orders")),
                  records(table("products")), records(table("customers")))
    return pd.DataFrame(res["orders"], columns=OB.COLUMNS)


def check_source() -> dict:
    """Load every table from the current source; returns row counts (raises DataSourceError)."""
    return {t: len(table(t)) for t in TABLES}


def records(df: pd.DataFrame) -> list[dict]:
    return df.replace({np.nan: None}).to_dict(orient="records")


@lru_cache(maxsize=1)
def historical_alerts() -> list[dict]:
    return scan_all_machines(table("sensor_readings"))


@lru_cache(maxsize=1)
def defects_enriched() -> pd.DataFrame:
    d = table("defect_records")
    o = table("production_orders")[["order_id", "product_id", "machine_id", "qty_produced"]]
    i = table("quality_inspections")[["inspection_id", "characteristic", "timestamp"]]
    p = table("process_records")[["order_id", "shift", "operator_id"]]
    m = table("machines")[["machine_id", "name", "line"]]
    out = (d.merge(o, on="order_id", how="left").merge(i, on="inspection_id", how="left")
            .merge(p, on="order_id", how="left").merge(m, on="machine_id", how="left"))
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    return out.sort_values("timestamp", ascending=False)


def estimate_copq_for(defects: pd.DataFrame, units_at_risk: int = 0) -> dict:
    n = len(defects)
    crit = int((defects["severity"] == "Critical").sum()) if n else 0
    return calculate_copq(
        scrap_units=n * 5, unit_cost=25.0, rework_hours=n * 0.5,
        sorting_hours=max(8.0, units_at_risk / 400), downtime_hours=4.0 + crit * 2,
        warranty_claims=crit, returns_units=int(units_at_risk * 0.002),
        investigation_hours=40.0,
    )


# ---------------------------------------------------------------------------
# Command Center
# ---------------------------------------------------------------------------

def overview() -> dict:
    machines = table("machines")
    insp = table("quality_inspections")
    defects = defects_enriched()
    alerts = historical_alerts()
    alert_by_machine: dict[str, dict] = {}
    for a in alerts:
        alert_by_machine.setdefault(a["machine_id"], a)

    total = len(insp)
    fails = int((insp["pass_fail"] == "FAIL").sum())

    # Weekly defect trend split by severity
    wk = defects.copy()
    wk["week"] = wk["timestamp"].dt.to_period("W-SUN").dt.start_time.dt.strftime("%Y-%m-%d")
    trend = (wk.groupby(["week", "severity"]).size().unstack(fill_value=0)
               .reindex(columns=["Critical", "Major", "Minor"], fill_value=0).reset_index())
    insp_wk = insp.copy()
    insp_wk["week"] = pd.to_datetime(insp_wk["timestamp"]).dt.to_period("W-SUN").dt.start_time.dt.strftime("%Y-%m-%d")
    weeks = sorted(insp_wk["week"].unique())
    trend = trend.set_index("week").reindex(weeks, fill_value=0).reset_index().rename(columns={"index": "week"})

    pareto = defects["defect_type"].value_counts()
    cum = (pareto.cumsum() / pareto.sum() * 100).round(1)
    pareto_rows = [{"type": k, "count": int(v), "cum_pct": float(cum[k])} for k, v in pareto.items()]

    shift_orders = table("process_records").groupby("shift")["order_id"].nunique()
    shift_defects = defects.groupby("shift").size()
    shifts = [{"shift": s, "defects": int(shift_defects.get(s, 0)), "orders": int(shift_orders.get(s, 0)),
               "defects_per_100_orders": round(shift_defects.get(s, 0) / shift_orders.get(s, 1) * 100, 1)}
              for s in ["Morning", "Afternoon", "Night"] if s in shift_orders.index]

    per_machine = defects.groupby("machine_id").size()
    sensor_machines = set(table("sensor_readings")["machine_id"].unique())
    fleet = []
    for _, m in machines.iterrows():
        a = alert_by_machine.get(m["machine_id"])
        risk = a["risk_level"] if a else ("low" if m["machine_id"] in sensor_machines else "unmonitored")
        fleet.append({
            "machine_id": m["machine_id"], "name": m["name"], "type": m["type"], "line": m["line"],
            "station": m["station"], "defects": int(per_machine.get(m["machine_id"], 0)),
            "risk": risk, "has_sensors": m["machine_id"] in sensor_machines,
            "alert": a,
        })

    lots = table("material_lots")
    bom = table("bom_consumption")
    failed_lots = lots[lots["coa_status"] != "PASS"]
    defect_orders = set(defects["order_id"])
    sup_rows = []
    for sid, grp in lots.groupby("supplier_id"):
        lot_ids = set(grp["lot_id"])
        orders = set(bom[bom["material_lot_id"].isin(lot_ids)]["order_id"])
        sup_rows.append({"supplier_id": sid, "lots": len(lot_ids),
                         "coa_failures": int((grp["coa_status"] != "PASS").sum()),
                         "defective_orders": len(orders & defect_orders)})
    sup_names = table("suppliers").set_index("supplier_id")["name"].to_dict()
    for r in sup_rows:
        r["name"] = sup_names.get(r["supplier_id"], r["supplier_id"])
    sup_rows.sort(key=lambda r: (-r["defective_orders"], -r["coa_failures"]))

    copq = estimate_copq_for(defects)
    weekly_fail = (insp_wk.assign(fail=insp_wk["pass_fail"] == "FAIL").groupby("week")["fail"].mean() * 100).round(2)

    return {
        "kpis": {
            "defects": len(defects),
            "critical_defects": int((defects["severity"] == "Critical").sum()),
            "open_containment": int((defects["containment_status"] == "Open").sum()),
            "inspections": total,
            "fail_rate_pct": round(fails / total * 100, 2) if total else 0,
            "first_pass_yield_pct": round(100 - fails / total * 100, 2) if total else 0,
            "machines_at_risk": len(alert_by_machine),
            "machines_total": len(machines),
            "copq_estimate": copq["total_copq"],
        },
        "spark": {
            "defects": [int(r) for r in trend[["Critical", "Major", "Minor"]].sum(axis=1)],
            "fail_rate": [float(v) for v in weekly_fail.values],
        },
        "trend": records(trend),
        "pareto": pareto_rows,
        "shifts": shifts,
        "fleet": fleet,
        "alerts": alerts,
        "suppliers": sup_rows[:6],
        "failed_lots": int(len(failed_lots)),
        "recent_defects": records(defects.head(12)[[
            "defect_id", "defect_type", "severity", "machine_id", "line", "shift", "order_id",
            "characteristic", "containment_status", "timestamp"]].assign(
                timestamp=lambda x: x["timestamp"].dt.strftime("%Y-%m-%d"))),
        "copq": copq,
    }


# ---------------------------------------------------------------------------
# Sensor history / SPC
# ---------------------------------------------------------------------------

def sensor_history(machine_id: str, sensor: str, points: int = 240) -> dict:
    s = table("sensor_readings")
    sub = s[s["machine_id"] == machine_id].sort_values("timestamp").tail(points)
    return {
        "machine_id": machine_id, "sensor": sensor,
        "series": [{"t": t, "v": float(v)} for t, v in zip(sub["timestamp"], sub[sensor])],
        "thresholds": SENSOR_THRESHOLDS.get(sensor, {}),
        "rul": estimate_rul(s, machine_id, sensor),
    }


def spc(characteristic: str, machine_id: str | None = None) -> dict:
    insp = table("quality_inspections").merge(
        table("production_orders")[["order_id", "machine_id"]], on="order_id", how="left")
    sub = insp[insp["characteristic"] == characteristic]
    if machine_id:
        sub = sub[sub["machine_id"] == machine_id]
    sub = sub.sort_values("timestamp")
    if len(sub) < 10:
        return {"error": f"Only {len(sub)} measurements for this selection; need at least 10."}
    values = sub["measured_value"].astype(float).values
    result = run_spc_analysis(values, float(sub["usl"].iloc[0]), float(sub["lsl"].iloc[0]))
    viol_idx = {v["index"] for v in result["nelson_violations"] if v["rule"] == 1}
    result["points"] = [
        {"i": i, "t": t, "v": float(v), "inspection_id": iid, "machine_id": mid, "fail": pf == "FAIL",
         "violation": i in viol_idx}
        for i, (t, v, iid, mid, pf) in enumerate(zip(sub["timestamp"], values, sub["inspection_id"],
                                                     sub["machine_id"], sub["pass_fail"]))
    ]
    hist, edges = np.histogram(values, bins=24)
    result["histogram"] = [{"x": round(float((edges[i] + edges[i + 1]) / 2), 4), "count": int(c)}
                           for i, c in enumerate(hist)]
    result["nominal"] = float(sub["nominal"].iloc[0])
    result["machine_id"] = machine_id
    result["characteristic"] = characteristic
    return result


def characteristics() -> list[str]:
    return sorted(table("quality_inspections")["characteristic"].dropna().unique().tolist())


def traceability(defect_id: str) -> dict:
    return trace_full(
        defect_id, table("defect_records"), table("quality_inspections"),
        table("production_orders"), table("bom_consumption"), table("material_lots"),
        table("suppliers"), table("shipments"), table("customers"),
    )


# ---------------------------------------------------------------------------
# Evidence packs used by the 8D agents (all computed from real tables)
# ---------------------------------------------------------------------------

def machine_profile(machine_id: str) -> dict:
    m = table("machines")
    row = m[m["machine_id"] == machine_id]
    return records(row)[0] if not row.empty else {"machine_id": machine_id}


def sensor_health(machine_id: str) -> list[dict]:
    s = table("sensor_readings")
    if machine_id not in set(s["machine_id"]):
        return []
    return [estimate_rul(s, machine_id, sensor) for sensor in SENSORS]


def maintenance_history(machine_id: str, n: int = 6) -> dict:
    logs = table("maintenance_logs")
    sub = logs[logs["machine_id"] == machine_id].sort_values("date", ascending=False)
    parts = table("parts_inventory")
    compat = parts[parts["machine_compatibility"].fillna("").str.contains(machine_id)]
    return {
        "recent": records(sub.head(n)[["date", "type", "description", "component_replaced", "duration_hrs", "cost_usd"]]),
        "total_events": len(sub),
        "unplanned_events": int((sub["type"] != "Scheduled").sum()),
        "total_cost_usd": round(float(sub["cost_usd"].sum()), 2),
        "spare_parts": records(compat[["part_id", "part_name", "qty_on_hand", "reorder_point", "unit_cost_usd"]]),
    }


def defect_breakdown(machine_id: str) -> dict:
    d = defects_enriched()
    mine = d[d["machine_id"] == machine_id]
    others = d[d["machine_id"] != machine_id]
    orders = table("production_orders")
    insp = table("quality_inspections").merge(orders[["order_id", "machine_id"]], on="order_id")
    m_insp = insp[insp["machine_id"] == machine_id]
    o_insp = insp[insp["machine_id"] != machine_id]
    return {
        "machine_defects": len(mine),
        "plant_defects": len(d),
        "share_of_plant_pct": round(len(mine) / len(d) * 100, 1) if len(d) else 0,
        "machine_fail_rate_pct": round((m_insp["pass_fail"] == "FAIL").mean() * 100, 2) if len(m_insp) else 0,
        "rest_of_plant_fail_rate_pct": round((o_insp["pass_fail"] == "FAIL").mean() * 100, 2) if len(o_insp) else 0,
        "by_type": mine["defect_type"].value_counts().to_dict(),
        "by_severity": mine["severity"].value_counts().to_dict(),
        "by_shift": mine["shift"].value_counts().to_dict(),
        "by_product": mine["product_id"].value_counts().to_dict(),
        "by_characteristic": mine["characteristic"].value_counts().to_dict(),
        "first_seen": str(mine["timestamp"].min().date()) if len(mine) else None,
        "last_seen": str(mine["timestamp"].max().date()) if len(mine) else None,
        "units_produced_on_defective_orders": int(mine.drop_duplicates("order_id")["qty_produced"].sum()) if len(mine) else 0,
        "defect_ids": mine.sort_values("severity", key=lambda s: s.map(SEVERITY_ORDER))["defect_id"].tolist(),
    }


def team_candidates(machine_id: str) -> list[dict]:
    p = table("process_records")
    ops = table("operators")
    ids = p[p["machine_id"] == machine_id]["operator_id"].value_counts().head(6).index
    sub = ops[ops["operator_id"].isin(ids)]
    return records(sub[["operator_id", "name", "shift", "certifications", "training_status"]])


def supplier_exposure(machine_id: str) -> list[dict]:
    d = defects_enriched()
    orders = set(d[d["machine_id"] == machine_id]["order_id"])
    bom = table("bom_consumption")
    lots = table("material_lots").merge(table("suppliers")[["supplier_id", "name", "rating"]], on="supplier_id")
    used = bom[bom["order_id"].isin(orders)]["material_lot_id"].value_counts()
    sub = lots[lots["lot_id"].isin(used.index)].copy()
    sub["defective_orders_using_lot"] = sub["lot_id"].map(used).astype(int)
    return records(sub.sort_values("defective_orders_using_lot", ascending=False)
                   [["lot_id", "supplier_id", "name", "material", "coa_status", "spec_grade", "rating",
                     "defective_orders_using_lot"]].head(8))


def similar_machines(machine_id: str) -> list[dict]:
    m = table("machines")
    row = m[m["machine_id"] == machine_id]
    if row.empty:
        return []
    mtype = row.iloc[0]["type"]
    alerts = {a["machine_id"]: a for a in historical_alerts()}
    peers = m[(m["type"] == mtype) & (m["machine_id"] != machine_id)]
    return [{"machine_id": r["machine_id"], "name": r["name"], "line": r["line"],
             "risk": alerts.get(r["machine_id"], {}).get("risk_level", "low")} for _, r in peers.iterrows()]


def spc_for_machine(machine_id: str) -> dict:
    bd = defect_breakdown(machine_id)
    char = next(iter(bd["by_characteristic"]), None) or "Outer Diameter"
    res = spc(char, machine_id)
    if "error" in res:
        res = spc(char)
    cap = res.get("capability", {})
    return {
        "characteristic": char, "scope": machine_id if res.get("machine_id") else "plant",
        "cpk": cap.get("cpk"), "cp": cap.get("cp"), "ppm": cap.get("ppm_estimate"), "verdict": cap.get("verdict"),
        "mean": cap.get("mean"), "std": cap.get("std"), "usl": cap.get("usl"), "lsl": cap.get("lsl"),
        "n": cap.get("sample_size"),
        "violations": [{"rule": v["rule"], "description": v["description"]} for v in res.get("nelson_violations", [])][:6],
    }


# ---------------------------------------------------------------------------
# Business: capacity, order book, OEE
# ---------------------------------------------------------------------------

HOURS_PER_DAY = 24.0  # three shifts (OPERATORS has Morning, Afternoon and Night crews)


@lru_cache(maxsize=1)
def machine_rates() -> dict[str, float]:
    """Demonstrated output in units per hour for every machine (orderbook.daily_rates over 24 h)."""
    daily = OB.daily_rates(records(table("machines")), records(table("production_orders")))
    return {k: round(v / HOURS_PER_DAY, 2) for k, v in daily.items()}


def order_book() -> list[dict]:
    return records(table("order_book"))


def customer_names() -> dict[str, str]:
    c = table("customers")
    return dict(zip(c["customer_id"], c["name"])) if len(c) else {}


@lru_cache(maxsize=1)
def oee_history() -> list[dict]:
    """OEE per machine over the order history: availability (maintenance downtime against 24 h days),
    performance (units produced against planned) and quality (inspection pass rate), with the hours lost to each."""
    machines = table("machines")
    orders = table("production_orders")
    logs = table("maintenance_logs")
    insp = table("quality_inspections").merge(orders[["order_id", "machine_id"]], on="order_id", how="left")
    if len(orders):
        days = (pd.to_datetime(orders["start_date"]).max() - pd.to_datetime(orders["start_date"]).min()).days + 1
    else:
        days = 90
    sched = days * HOURS_PER_DAY
    rates = machine_rates()
    out = []
    for m in records(machines):
        mid = m["machine_id"]
        down = float(logs.loc[logs["machine_id"] == mid, "duration_hrs"].sum())
        o = orders[orders["machine_id"] == mid]
        i = insp[insp["machine_id"] == mid]
        a = max(0.0, 1 - down / sched)
        planned, made = float(o["qty_planned"].sum()), float(o["qty_produced"].sum())
        p = min(made / planned, 1.0) if planned else None
        q = float((i["pass_fail"] == "PASS").mean()) if len(i) else None
        rate = rates.get(mid) or 1.0
        out.append({"machine_id": mid, "name": m["name"], "type": m["type"], "line": m["line"],
                    "availability": round(a, 4), "performance": round(p, 4) if p is not None else None,
                    "quality": round(q, 4) if q is not None else None,
                    "oee": round(a * p * q, 4) if p is not None and q is not None else None,
                    "downtime_h": round(down, 1), "scheduled_h": sched, "units": int(made),
                    "loss_availability_h": round(down, 1),
                    "loss_performance_h": round(max(planned - made, 0) / rate, 1) if p is not None else 0.0,
                    "loss_quality_h": round(made * (1 - q) / rate, 1) if q is not None else 0.0,
                    "rate_h": rate})
    return out
