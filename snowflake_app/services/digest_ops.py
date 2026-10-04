"""
Daily digest from the app: the same email the EIGHT_D_DAILY_DIGEST task sends
every morning (core/digest.py), built from the app's live state for the
preview and "Send now" (with the configured transport, so SMTP and SendGrid
work too), plus reading and changing the task's schedule.
"""

import re
import time

from snowflake_app.core import digest as G
from snowflake_app.services import capa_ops, impact_ops, mailer, parts_ops
from snowflake_app.services.llm import SnowflakePool
from snowflake_app.services.settings import names

OPEN = ("planned", "awaiting", "in_repair")
_task_cache: dict = {}


def build(mon, settings: dict) -> dict:
    now = time.time()
    wos = mon.work_orders()
    outlook = mon.outlook()
    plan = parts_ops.plan(outlook, [w for w in wos if w["status"] in OPEN], now)
    stock = [{"part_id": p["part_id"], "part_name": p["part_name"], "reorder_point": p["reorder_point"],
              "vendor_name": p["vendor_name"], "lead_time_days": p["lead_time_days"],
              "incoming": (plan["incoming"].get(p["part_id"]) or {}).get("qty", 0), **plan["stock"][p["part_id"]]}
             for p in plan["master"]]
    biz = impact_ops.plant(outlook, wos, settings, now)
    risk = []
    for r in biz["down"] + biz["forecast"]:
        sc = r["scenario"]
        if sc["late"] or sc["tight_count"]:
            risk.append({"machine_id": r["machine_id"], "name": r["name"], "kind": sc["kind"], "late": sc["late"],
                         "tight": sc["tight_count"], "value_at_risk": sc["value_at_risk"], "penalty_usd": sc["penalty_usd"],
                         "customers": sc["customers"], "total_cost": sc["total_cost"]})
    return G.build(capa_ops.actions(), stock, parts_ops.pos(), risk, now)


def lists() -> list[dict]:
    return [x for x in mailer.lists() if x.get("daily_digest")]


def send_now(mon, settings: dict) -> dict:
    """Send today's digest to every list marked Daily digest. {sent: [names], failed: {name: error}, counts}."""
    targets = lists()
    if not targets:
        raise mailer.EmailError("No recipient list is marked Daily digest (Settings > Email & recipients).")
    d = build(mon, settings)
    sent, failed = [], {}
    for lst in targets:
        try:
            mailer.send_html(settings, lst["emails"], d["subject"], d["html"], reason="daily digest", list_name=lst["name"])
            sent.append(lst["name"])
        except mailer.EmailError as e:
            failed[lst["name"]] = str(e)
    return {"sent": sent, "failed": failed, "counts": d["counts"]}


def task(settings: dict, force: bool = False) -> dict:
    """{exists, state, schedule, hour, tz, error} for the EIGHT_D_DAILY_DIGEST task (cached for a minute)."""
    sf = settings["providers"]["snowflake"]
    n = names()
    key = (n["digest_task"], sf.get("mode"), sf.get("connection_name"))
    hit = _task_cache.get(key)
    if hit and not force and time.time() - hit[0] < 60:
        return hit[1]
    db, schema, name = n["digest_task"].split(".")
    try:
        df = SnowflakePool.query_df(sf, f"SHOW TASKS LIKE '{name}' IN SCHEMA {db}.{schema}")
    except Exception as e:
        return {"exists": False, "error": str(e).splitlines()[0][:200]}
    if df.empty:
        res = {"exists": False, "error": None}
    else:
        row = {str(k).lower(): v for k, v in df.iloc[0].items()}
        m = re.match(r"USING CRON (\d+) (\d+) \* \* \* (\S+)", str(row.get("schedule") or ""))
        res = {"exists": True, "state": row.get("state"), "schedule": row.get("schedule"), "error": None,
               "minute": int(m[1]) if m else 0, "hour": int(m[2]) if m else None, "tz": m[3] if m else "UTC"}
    _task_cache[key] = (time.time(), res)
    return res


def set_hour(settings: dict, hour: int, tz: str | None = None) -> dict:
    """Move the daily digest to `hour` in time zone `tz` (default: the task's current one) and keep it running."""
    t = task(settings, force=True)
    if not t.get("exists"):
        raise RuntimeError(t.get("error") or "The daily digest task does not exist yet: run python sql/setup.py --steps business")
    tz = tz or t["tz"]
    import zoneinfo
    zones = zoneinfo.available_timezones()  # empty when the runtime has no tz database: fall back to the name's shape
    if (zones and tz not in zones and tz != "UTC") or not re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$", tz):
        raise ValueError(f"Unknown time zone {tz!r}")
    sf = settings["providers"]["snowflake"]
    full = names()["digest_task"]
    hour = max(0, min(int(hour), 23))
    SnowflakePool.query(sf, f"ALTER TASK {full} SUSPEND")
    SnowflakePool.query(sf, f"ALTER TASK {full} SET SCHEDULE = 'USING CRON {t['minute']} {hour} * * * {tz}'")
    SnowflakePool.query(sf, f"ALTER TASK {full} RESUME")
    return task(settings, force=True)
