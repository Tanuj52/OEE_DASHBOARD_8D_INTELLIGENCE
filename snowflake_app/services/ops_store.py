"""
Operations store: machine state, work orders, events, recipient lists, the
email log, finished 8D investigations, the plant-wide app settings, corrective
actions, the spare-parts ledger and purchase orders.

Reads are served from an in-memory mirror loaded once per configuration, so
pages never wait on Snowflake. Writes update the mirror immediately and are
persisted by a single background writer:

  snowflake - MERGE into <app database>.OPERATIONS (or the schema set in
              Settings). Writes happen only when something changes (a machine
              changes state, a work order moves, a list is edited), so the
              warehouse can auto-suspend between them.
  local     - JSON files under snowflake_app/.ops (used when the data source is
              the bundled files, or the schema cannot be created).

Tables are created on first use when the role allows it; sql/07_operations.sql
has the same DDL for an administrator to run.
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime

from snowflake_app.services.settings import names

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.environ.get("OPS_DIR") or os.path.join(APP_DIR, ".ops")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")

# table -> (key, [(column, type)]); every table also keeps the full record in DATA
TABLES = {
    "machine_state": ("machine_id", [
        ("machine_id", "STRING"), ("machine_name", "STRING"), ("line", "STRING"), ("lifecycle", "STRING"),
        ("status", "STRING"), ("condition", "STRING"), ("fault_mode", "STRING"), ("fault_sensor", "STRING"),
        ("health_pct", "FLOAT"), ("rul_hours", "FLOAT"), ("model_version", "NUMBER"), ("work_order_id", "STRING"),
        ("down_since", "TIMESTAMP_TZ"), ("updated_at", "TIMESTAMP_TZ")]),
    "work_orders": ("wo_id", [
        ("wo_id", "STRING"), ("machine_id", "STRING"), ("machine_name", "STRING"), ("line", "STRING"), ("kind", "STRING"),
        ("priority", "STRING"), ("status", "STRING"), ("failure_mode", "STRING"), ("component", "STRING"),
        ("sensor", "STRING"), ("reason", "STRING"), ("created_at", "TIMESTAMP_TZ"), ("down_at", "TIMESTAMP_TZ"),
        ("started_at", "TIMESTAMP_TZ"), ("completed_at", "TIMESTAMP_TZ"), ("closed_at", "TIMESTAMP_TZ"),
        ("rul_hours_at_open", "FLOAT"), ("est_hours", "FLOAT"), ("est_cost_usd", "FLOAT"), ("downtime_hours", "FLOAT"),
        ("technician_id", "STRING"), ("technician_name", "STRING"), ("notes", "STRING"), ("run_id", "STRING")]),
    "recipient_lists": ("name", [
        ("name", "STRING"), ("emails", "ARRAY"), ("on_report", "BOOLEAN"), ("on_machine_down", "BOOLEAN"),
        ("is_default", "BOOLEAN"), ("updated_at", "TIMESTAMP_TZ")]),
    "email_log": ("email_id", [
        ("email_id", "STRING"), ("sent_at", "TIMESTAMP_TZ"), ("run_id", "STRING"), ("list_name", "STRING"),
        ("recipients", "ARRAY"), ("subject", "STRING"), ("transport", "STRING"), ("status", "STRING"),
        ("error", "STRING"), ("reason", "STRING")]),
    # finished 8D runs (the full report snapshot is in DATA), so reports survive app restarts
    "investigations": ("id", [
        ("id", "STRING"), ("created_at", "TIMESTAMP_TZ"), ("finished_at", "TIMESTAMP_TZ"), ("status", "STRING"),
        ("machine_id", "STRING"), ("source", "STRING"), ("decision", "STRING"), ("total_tokens", "NUMBER")]),
    # plant-wide settings shared by every viewer (services/settings.SHARED; no secrets)
    "app_settings": ("name", [("name", "STRING"), ("updated_at", "TIMESTAMP_TZ"), ("updated_by", "STRING")]),
    # corrective and preventive actions from finished 8D reports (services/capa_ops.py)
    "capa_actions": ("action_id", [
        ("action_id", "STRING"), ("run_id", "STRING"), ("machine_id", "STRING"), ("discipline", "STRING"),
        ("action", "STRING"), ("owner_id", "STRING"), ("owner_name", "STRING"), ("owner_role", "STRING"),
        ("status", "STRING"), ("due_at", "TIMESTAMP_TZ"), ("created_at", "TIMESTAMP_TZ"), ("done_at", "TIMESTAMP_TZ"),
        ("verified_at", "TIMESTAMP_TZ"), ("result", "STRING")]),
    # spare-part stock movements: opening balance is PRODUCTION.PARTS_INVENTORY (services/parts_ops.py)
    "part_transactions": ("txn_id", [
        ("txn_id", "STRING"), ("ts", "TIMESTAMP_TZ"), ("part_id", "STRING"), ("kind", "STRING"), ("qty", "NUMBER"),
        ("wo_id", "STRING"), ("po_id", "STRING"), ("user_name", "STRING"), ("note", "STRING")]),
    "purchase_orders": ("po_id", [
        ("po_id", "STRING"), ("part_id", "STRING"), ("part_name", "STRING"), ("vendor_name", "STRING"), ("qty", "NUMBER"),
        ("unit_cost_usd", "FLOAT"), ("status", "STRING"), ("reason", "STRING"), ("wo_id", "STRING"),
        ("created_at", "TIMESTAMP_TZ"), ("ordered_at", "TIMESTAMP_TZ"), ("expected_at", "TIMESTAMP_TZ"),
        ("received_at", "TIMESTAMP_TZ"), ("created_by", "STRING")]),
}
LOAD_ORDER = {"email_log": "SENT_AT", "investigations": "CREATED_AT"}
OPEN_SQL = "'planned', 'awaiting', 'in_repair', 'commissioning'"
EVENTS = [("event_id", "STRING"), ("event_ts", "TIMESTAMP_TZ"), ("machine_id", "STRING"), ("level", "STRING"),
          ("kind", "STRING"), ("message", "STRING"), ("detail", "STRING"), ("session_id", "STRING")]
EVENT_KEEP = 2000
MAX_RETRIES = 5


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _col_expr(col: str, typ: str, src: str = "s.d") -> str:
    if typ == "TIMESTAMP_TZ":
        return f"TRY_TO_TIMESTAMP_TZ({src}:{col}::STRING)"
    if typ == "ARRAY":
        return f"{src}:{col}::ARRAY"
    if typ == "STRING":
        return f"{src}:{col}::STRING"
    return f"TRY_CAST({src}:{col}::STRING AS {typ})"


def ddl(schema: str) -> list[str]:
    out = [f"CREATE SCHEMA IF NOT EXISTS {schema} COMMENT = '8D Intelligence: live operations state written by the app'"]
    for t, (key, cols) in TABLES.items():
        body = ", ".join(f"{c.upper()} {typ}" for c, typ in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{t.upper()} ({body}, DATA VARIANT, PRIMARY KEY ({key.upper()}))")
    body = ", ".join(f"{c.upper()} {typ}" for c, typ in EVENTS)
    out.append(f"CREATE TABLE IF NOT EXISTS {schema}.MACHINE_EVENTS ({body}, DATA VARIANT)")
    return out


def merge_sql(schema: str, table: str) -> str:
    key, cols = TABLES[table]
    upd = ", ".join(f"t.{c.upper()} = {_col_expr(c, typ)}" for c, typ in cols if c != key)
    names = ", ".join(c.upper() for c, _ in cols)
    vals = ", ".join(_col_expr(c, typ) for c, typ in cols)
    return (f"MERGE INTO {schema}.{table.upper()} t USING (SELECT PARSE_JSON(%s) AS d) s "
            f"ON t.{key.upper()} = s.d:{key}::STRING "
            f"WHEN MATCHED THEN UPDATE SET {upd}, t.DATA = s.d "
            f"WHEN NOT MATCHED THEN INSERT ({names}, DATA) VALUES ({vals}, s.d)")


def events_sql(schema: str) -> str:
    names = ", ".join(c.upper() for c, _ in EVENTS)
    vals = ", ".join(_col_expr(c, typ, "f.value") for c, typ in EVENTS)
    return f"INSERT INTO {schema}.MACHINE_EVENTS ({names}, DATA) SELECT {vals}, f.value FROM TABLE(FLATTEN(PARSE_JSON(%s))) f"


def _clean(v):
    if isinstance(v, float) and v != v or v in (float("inf"), float("-inf")):
        return None  # NaN/inf are not valid JSON for PARSE_JSON
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _json(v) -> str:
    return json.dumps(_clean(v), default=str)


class _LocalSink:
    kind = "local"

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "state.json")
        self.events_path = os.path.join(folder, "events.jsonl")

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, mirror: dict, events: list[dict], changed: set):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(mirror, f, default=str)
        os.replace(tmp, self.path)
        if events:
            with open(self.events_path, "a") as f:
                for e in events:
                    f.write(json.dumps(e, default=str) + "\n")
        return set()


class _SnowflakeSink:
    kind = "snowflake"

    def __init__(self, sf: dict, schema: str):
        from snowflake_app.services.llm import SnowflakePool

        self.pool = SnowflakePool
        self.sf = sf
        self.schema = schema.upper()
        for stmt in ddl(self.schema):
            self.pool.query(sf, stmt)

    def load(self) -> dict:
        out = {}
        for t in TABLES:
            key = TABLES[t][0]
            if t == "work_orders":  # every open order, however old, plus the newest 500 others
                sql = (f"SELECT DATA FROM {self.schema}.WORK_ORDERS WHERE STATUS IN ({OPEN_SQL}) UNION ALL "
                       f"SELECT DATA FROM (SELECT DATA FROM {self.schema}.WORK_ORDERS WHERE STATUS NOT IN ({OPEN_SQL}) "
                       "OR STATUS IS NULL ORDER BY CREATED_AT DESC LIMIT 500)")
            else:
                order = f" ORDER BY {LOAD_ORDER[t]} DESC LIMIT 500" if t in LOAD_ORDER else ""
                sql = f"SELECT DATA FROM {self.schema}.{t.upper()}{order}"
            rows = self.pool.query(self.sf, sql)
            recs = [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]
            out[t] = {str(r[key]): r for r in recs if r and r.get(key) is not None}
        return out

    def write(self, mirror: dict, events: list[dict], changed: set) -> set:
        """Write each changed record on its own, so one bad record cannot block the rest.
        Returns the (table, key) pairs that failed; raises only when nothing could be written."""
        failed, err = set(), None
        for table, key in sorted(changed):
            rec = mirror.get(table, {}).get(key)
            try:
                if rec is None:
                    k = TABLES[table][0]
                    self.pool.query(self.sf, f"DELETE FROM {self.schema}.{table.upper()} WHERE {k.upper()} = %s", (key,))
                else:
                    self.pool.query(self.sf, merge_sql(self.schema, table), (_json(rec),))
            except Exception as e:
                failed.add((table, key))
                err = e
        if events:
            self.pool.query(self.sf, events_sql(self.schema), (_json(events),))
        if failed and len(failed) == len(changed):
            raise err
        return failed


class OpsStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.mirror: dict[str, dict] = {t: {} for t in TABLES}
        self.events: list[dict] = []
        self.sink = None
        self.config_key = None
        self.error = None
        self._changed: set = set()
        self._pending_events: list[dict] = []
        self._wake = queue.Queue()
        self._thread = None
        self.last_write = None
        self.write_error = None
        self._failures: dict = {}
        self._config_lock = threading.Lock()

    # -- configuration -----------------------------------------------------
    def configure(self, settings: dict) -> dict:
        """Pick the backend for the configured data source. Cheap when nothing changed."""
        ops = settings.get("ops") or {}
        sf = dict(settings["providers"]["snowflake"])
        use_sf = (os.environ.get("OPS_STORE") != "local" and settings["data"]["source"] == "snowflake"
                  and ops.get("enabled", True))
        schema = (ops.get("schema") or names()["ops_schema"]).strip()
        key = json.dumps([use_sf, schema.upper(), {k: sf.get(k) for k in ("mode", "connection_name", "account", "user", "role")}],
                         sort_keys=True)
        if key == self.config_key:
            return self.status()
        # Switching backends flushes, creates tables and loads over the network: do that without holding
        # self.lock, so the monitor thread (which writes through put()) never waits on Snowflake here.
        with self._config_lock:
            if key == self.config_key:
                return self.status()
            self.flush(5)
            sink, err = None, None
            if use_sf:
                try:
                    if not _IDENT.match(schema):
                        raise ValueError(f"'{schema}' is not a valid DATABASE.SCHEMA name")
                    sink = _SnowflakeSink(sf, schema)
                except Exception as e:
                    err = str(e).splitlines()[0][:300]
            if sink is None:
                sink = _LocalSink(LOCAL_DIR)
            try:
                data = sink.load()
            except Exception as e:
                err = err or str(e).splitlines()[0][:300]
                sink, data = _LocalSink(LOCAL_DIR), _LocalSink(LOCAL_DIR).load()
            with self.lock:
                self.sink, self.error, self.config_key = sink, err, key
                self.mirror = {t: dict((data or {}).get(t) or {}) for t in TABLES}
                self._changed.clear()
                self._pending_events.clear()
                self._failures.clear()
            self._ensure_writer()
            return self.status()

    def status(self) -> dict:
        s = self.sink
        return {"kind": s.kind if s else None, "schema": getattr(s, "schema", None), "error": self.error,
                "last_write": self.last_write, "write_error": self.write_error,
                "pending": len(self._changed) + len(self._pending_events)}

    # -- writes ------------------------------------------------------------
    def put(self, table: str, rec: dict):
        key = str(rec[TABLES[table][0]])
        with self.lock:
            self.mirror[table][key] = dict(rec)
            self._changed.add((table, key))
        self._wake.put(1)

    def delete(self, table: str, key: str):
        with self.lock:
            self.mirror[table].pop(key, None)
            self._changed.add((table, key))
        self._wake.put(1)

    def add_events(self, events: list[dict]):
        if not events:
            return
        with self.lock:
            self._pending_events.extend(events)
            self.events = (self.events + events)[-EVENT_KEEP:]
        self._wake.put(1)

    def _ensure_writer(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._writer, daemon=True, name="ops-writer")
            self._thread.start()

    def _writer(self):
        while True:
            try:
                self._wake.get(timeout=30)
                time.sleep(0.3)  # coalesce bursts (several state changes from one sample)
            except queue.Empty:
                if not (self._changed or self._pending_events):
                    continue  # idle; otherwise retry what failed last time
            self._drain()

    def _drain(self):
        with self.lock:
            sink = self.sink
            changed, self._changed = self._changed, set()
            events, self._pending_events = self._pending_events, []
            mirror = {t: dict(v) for t, v in self.mirror.items()}
        if not (changed or events) or sink is None:
            return
        try:
            failed = sink.write(mirror, events, changed) or set()
            self.last_write, self.write_error = now_iso(), None
            with self.lock:
                for k in failed:  # retry a few times, then give up on that record (it stays in memory)
                    self._failures[k] = self._failures.get(k, 0) + 1
                    if self._failures[k] < MAX_RETRIES:
                        self._changed.add(k)
                    else:
                        self.write_error = f"Could not save {k[0]} {k[1]} after {MAX_RETRIES} attempts"
                for k in changed - failed:
                    self._failures.pop(k, None)
        except Exception as e:
            self.write_error = str(e).splitlines()[0][:300]
            with self.lock:  # keep for the next attempt
                self._changed |= changed
                self._pending_events = events + self._pending_events

    def flush(self, timeout: float = 10.0):
        """Write everything pending now (used by tests and before switching backends)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if not (self._changed or self._pending_events) or self.sink is None:
                    return True
            self._drain()
            if self.write_error:
                return False
        return False

    # -- reads -------------------------------------------------------------
    def all(self, table: str) -> list[dict]:
        with self.lock:
            return [dict(v) for v in self.mirror[table].values()]

    def keys(self, table: str) -> list[str]:
        with self.lock:
            return list(self.mirror[table])

    def get(self, table: str, key: str) -> dict | None:
        with self.lock:
            v = self.mirror[table].get(str(key))
            return dict(v) if v else None


STORE = OpsStore()


def store() -> OpsStore:
    return STORE
