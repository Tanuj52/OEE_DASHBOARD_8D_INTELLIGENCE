"""
Live monitoring runner: drives the shared MonitoringEngine from a background
thread so telemetry keeps streaming while users move between pages or refresh
the browser. The UI reads consistent snapshots under a lock.

On the realtime clock the worker produces each sample when it falls due on the
wall clock and keeps streaming whether or not anyone is watching, like a plant
historian feed; a stream with no viewer for IDLE_STOP_S is stopped. On the
simulated clock an unwatched stream (no heartbeat for IDLE_PAUSE_S) pauses
itself and resumes on the next heartbeat.

The runner also owns the maintenance side of the plant:
  - machine state is loaded from and saved to the operations store, so a machine
    that tripped stays down across restarts and its wear timeline continues;
  - a protective stop raises an unplanned work order, a remaining-useful-life
    notice raises a planned one (when automation allows);
  - operators move work orders through repair, and completing a repair
    recommissions the machine;
  - automation hooks (8D launch, email alerts) run off the sampling thread.
"""

import copy
import threading
import time
from datetime import datetime

from snowflake_app.core import maintenance as MX
from snowflake_app.core.monitoring import DOWN, MonitoringEngine, fmt_hours
from snowflake_app.services import ops_store, parts_ops
from snowflake_app.services.settings import hosted

IDLE_PAUSE_S = 90
IDLE_STOP_S = 6 * 3600
IDLE_STOP_HOSTED_S = 20 * 60  # public deployment: stop soon after the last viewer leaves, so the warehouse can suspend
STATE_REFRESH_S = 300   # refresh stored health / time to failure this often (status changes are written at once)
OPEN_WO = ("planned", "awaiting", "in_repair", "commissioning")
PERSIST_KINDS = {"trip", "stopped", "repair_started", "repair_completed", "recommissioned", "escalate", "degradation",
                 "rul_plan", "rul_urgent", "retrain", "work_order"}


def _now_iso() -> str:
    return ops_store.now_iso()


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso)).timestamp() if iso else None
    except ValueError:
        return None


class LiveMonitor:
    def __init__(self):
        self.engine = MonitoringEngine()
        self.engine.on_event = self._on_event
        self.engine.on_state = self._on_state
        self.lock = threading.RLock()
        self.heartbeat = time.time()
        self.idle_paused = False
        self._thread: threading.Thread | None = None
        self.context: dict = {}          # settings snapshot taken when monitoring started
        self.hooks: dict = {}            # {"trip": fn(monitor, wo, event, settings), "work_order": fn(...)}
        self._deferred: list = []
        self._state_written = 0.0

    @property
    def store(self):
        return ops_store.store()

    # -- lifecycle ---------------------------------------------------------
    def start(self, cfg: dict, settings: dict | None = None) -> dict:
        settings = copy.deepcopy(settings or {})
        auto = settings.get("automation") or {}
        cfg = dict(cfg)
        if cfg.get("clock") == "realtime":
            cfg.setdefault("tiers", {"plan": auto.get("plan_hours", 72), "urgent": auto.get("urgent_hours", 24),
                                     "critical": auto.get("critical_hours", 4)})
            cfg.setdefault("trip_after_s", auto.get("trip_after_s", 10))
            cfg.setdefault("near_failure_machine", auto.get("near_failure_machine", True))
        stored = {r["machine_id"]: r for r in self.store.all("machine_state")} if cfg.get("clock") == "realtime" else {}
        with self.lock:
            self.context = settings
            state = self.engine.start(cfg, stored)
            self.idle_paused = False
            for wo in self.work_orders():  # orders left in commissioning by an earlier version or a crash
                m = self.engine.machines.get(wo["machine_id"])
                if wo["status"] == "commissioning" and (m is None or m.lifecycle != "commissioning"):
                    self._close(wo)
        self._run_deferred()
        self.touch()
        return state

    def stop(self):
        with self.lock:
            self.engine.stop()

    def set_paused(self, paused: bool):
        with self.lock:
            self.engine.set_paused(paused)
            self.idle_paused = False

    def set_speed(self, speed: float):
        with self.lock:
            self.engine.set_speed(speed)

    def touch(self):
        """Heartbeat from the UI; resumes an idle-paused stream and keeps the worker alive."""
        self.heartbeat = time.time()
        with self.lock:
            if self.idle_paused and self.engine.active:
                self.engine.set_paused(False)
                self.idle_paused = False
            if self.engine.active and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, daemon=True, name=f"monitor-{self.engine.session_id}")
                self._thread.start()

    def _loop(self):
        while True:
            with self.lock:
                eng = self.engine
                if not eng.active:
                    return
                now = time.time()
                if eng.realtime:
                    if now - self.heartbeat > (IDLE_STOP_HOSTED_S if hosted() else IDLE_STOP_S):
                        eng.stop()
                        return
                    eng.advance_to(now)
                    self._auto_complete(now)
                    if now - self._state_written >= STATE_REFRESH_S:
                        self._state_written = now
                        for m in eng.machines.values():
                            if m.lifecycle not in DOWN:
                                self.store.put("machine_state", eng.machine_record(m))
                    wait = min(eng.seconds_to_next(time.time()), 1.0) if not eng.paused else 0.5
                else:
                    if now - self.heartbeat > IDLE_PAUSE_S:
                        if not eng.paused:
                            eng.set_paused(True)
                            self.idle_paused = True
                        return
                    if not eng.paused:
                        eng.safe_step()
                    wait = eng.tick_interval()
            self._run_deferred()
            time.sleep(max(wait, 0.01))

    # -- engine callbacks (called with self.lock held) -------------------------
    def _on_state(self, rec: dict):
        self.store.put("machine_state", rec)

    def _on_event(self, ev: dict):
        if not self.engine.realtime:
            return
        if ev.get("level") in ("warning", "critical") or ev.get("kind") in PERSIST_KINDS:
            self.store.add_events([{"event_id": ev["id"], "event_ts": ev["sim_time"], "machine_id": ev.get("machine_id"),
                                    "level": ev["level"], "kind": ev.get("kind"), "message": ev["message"],
                                    "detail": ev.get("detail"), "session_id": ev.get("session_id")}])
        kind, mid = ev.get("kind"), ev.get("machine_id")
        auto = self.context.get("automation") or {}
        if kind == "trip":
            wo = self._raise_unplanned(mid, ev)
            self._defer("trip", wo, ev)
        elif kind in ("rul_plan", "rul_urgent") and auto.get("auto_plan", True):
            if not self.open_work_order(mid):
                wo = self._new_work_order(mid, "planned", reason=ev["message"], rul_hours=ev.get("rul_hours"))
                self._defer("work_order", wo, ev)
        elif kind == "recommissioned":
            wo = self.open_work_order(mid)
            if wo:
                self._close(wo)
            self.engine.set_work_order(mid, None)

    def _close(self, wo: dict):
        down = _epoch(wo.get("down_at"))
        end = _epoch(wo.get("completed_at")) or time.time()  # downtime ends when the machine runs again
        wo.update(status="closed", closed_at=_now_iso(),
                  downtime_hours=round(max(end - down, 0) / 3600, 2) if down else wo.get("downtime_hours"))
        self.store.put("work_orders", wo)

    def _defer(self, kind: str, wo: dict, ev: dict):
        fn = self.hooks.get(kind)
        if fn:
            self._deferred.append((fn, wo, ev))

    def _run_deferred(self):
        """Automation (8D launch, emails) runs on its own thread so sampling never waits on it."""
        with self.lock:
            jobs, self._deferred = self._deferred, []
            ctx = copy.deepcopy(self.context)
        for fn, wo, ev in jobs:
            threading.Thread(target=self._safe_hook, args=(fn, wo, ev, ctx), daemon=True, name="automation").start()

    def _safe_hook(self, fn, wo, ev, ctx):
        try:
            fn(self, wo, ev, ctx)
        except Exception as e:  # never let automation break monitoring
            with self.lock:
                self.engine._event("warning", wo.get("machine_id") if wo else None, f"Automation step failed: {str(e)[:160]}",
                                   kind="automation_error")

    # -- work orders -------------------------------------------------------
    def _machine_info(self, mid: str) -> dict:
        m = self.engine.machines.get(mid)
        if m:
            return {"machine_id": mid, "name": m.name, "line": m.line, "type": m.type}
        from snowflake_app.core import data as D
        rows = [r for r in D.records(D.table("machines")) if r["machine_id"] == mid]
        r = rows[0] if rows else {"machine_id": mid, "name": mid, "line": "", "type": ""}
        return {"machine_id": mid, "name": r.get("name"), "line": r.get("line"), "type": r.get("type")}

    def _new_work_order(self, mid: str, kind: str, *, reason: str, rul_hours=None, down_at=None) -> dict:
        m = self.engine.machines.get(mid)
        prof = (m.profile or {}) if m else {}
        rul = (m.rul or {}) if m else {}
        sensor = rul.get("sensor") or (m.drift or {}).get("sensor") if m else None
        wo = MX.build_work_order(self._machine_info(mid), kind, fault=prof.get("fault"),
                                 sensor=sensor or prof.get("sensor"), reason=reason,
                                 rul_hours=rul_hours if rul_hours is not None else rul.get("hours"),
                                 now_iso=_now_iso(), down_at=down_at)
        if kind == "planned" and wo.get("rul_hours_at_open") is not None:
            wo["lead_hours"] = round(float(wo["rul_hours_at_open"]), 3)  # notice given before the forecast failure
        elif m and m.first_warning_ms:
            ref = _epoch(down_at) or time.time()
            wo["lead_hours"] = round(max(ref - m.first_warning_ms / 1000, 0) / 3600, 3)
        self.store.put("work_orders", wo)
        self._parts(parts_ops.reserve_for_wo, wo)
        if m:
            self.engine.set_work_order(mid, wo["wo_id"])
            self.engine._event("info", mid, f"Work order {wo['wo_id']} ({wo['priority_label'].lower()}) raised for {mid}: "
                                            f"{wo['component'].lower()}, about {wo['est_hours']:g} h, ${wo['est_cost_usd']:,.0f}.",
                               kind="work_order", wo_id=wo["wo_id"])
        return wo

    def _parts(self, fn, *args):
        """Stock-ledger step for a work order; a ledger problem must never block the maintenance flow."""
        try:
            return fn(*args)
        except Exception as e:
            mid = args[0].get("machine_id") if args and isinstance(args[0], dict) else None
            self.engine._event("warning", mid, f"Spare-parts ledger not updated: {str(e).splitlines()[0][:160]}",
                               kind="automation_error")
            return None

    def _raise_unplanned(self, mid: str, ev: dict) -> dict:
        m = self.engine.machines.get(mid)
        down_at = datetime.fromtimestamp(m.down_since).astimezone().isoformat(timespec="seconds") if m and m.down_since else _now_iso()
        wo = self.open_work_order(mid)
        if wo and wo["status"] == "commissioning":
            self._close(wo)  # it ran again after the repair, so that job is done; this stop is a new one
            wo = None
        if wo and wo["status"] == "planned":
            # the planned job is now an emergency: the machine stopped before it was done
            wo.update(status="awaiting", kind="unplanned", priority="P1", priority_label=MX.PRIORITY["P1"], down_at=down_at,
                      reason=ev["message"], avoided_cost_usd=0, avoided_hours=0,
                      est_hours=MX.estimate(mid, "unplanned")["hours"], est_cost_usd=MX.estimate(mid, "unplanned")["cost_usd"])
            if m and m.first_warning_ms and m.down_since:
                wo["lead_hours"] = round(max(m.down_since - m.first_warning_ms / 1000, 0) / 3600, 3)
            self.store.put("work_orders", wo)
            if m:
                self.engine.set_work_order(mid, wo["wo_id"])
            return wo
        if wo:
            return wo
        return self._new_work_order(mid, "unplanned", reason=ev["message"], rul_hours=0, down_at=down_at)

    def work_orders(self) -> list[dict]:
        return sorted(self.store.all("work_orders"), key=lambda w: w.get("created_at") or "", reverse=True)

    def open_work_order(self, mid: str) -> dict | None:
        return next((w for w in self.work_orders() if w["machine_id"] == mid and w["status"] in OPEN_WO), None)

    def link_run(self, wo_id: str, run_id: str):
        with self.lock:  # called from automation threads; the monitor may be moving the same order
            wo = self.store.get("work_orders", wo_id)
            if wo:
                wo["run_id"] = run_id
                self.store.put("work_orders", wo)

    def _engine_has(self, mid: str) -> bool:
        """The machine is in a running stream (a stopped session's machines are handled as offline)."""
        return bool(self.engine.active) and mid in self.engine.machines

    def _offline_state(self, mid: str, **changes):
        """Monitoring is off: record the change on the stored machine state for the next session."""
        rec = self.store.get("machine_state", mid) or {"machine_id": mid, "profile": {}}
        rec.update(changes, updated_at=_now_iso())
        self.store.put("machine_state", rec)

    # -- operator actions ----------------------------------------------------
    def schedule_maintenance(self, mid: str) -> dict:
        with self.lock:
            wo = self.open_work_order(mid)
            if wo:
                return wo
            m = self.engine.machines.get(mid) if self._engine_has(mid) else None
            stored = self.store.get("machine_state", mid) or {}
            lifecycle = m.lifecycle if m else stored.get("lifecycle")
            if lifecycle in DOWN:  # already stopped: this is a breakdown job, timed from the stop
                since = (datetime.fromtimestamp(m.down_since).astimezone().isoformat(timespec="seconds")
                         if m and m.down_since else stored.get("down_since") or _now_iso())
                wo = self._new_work_order(mid, "unplanned", reason=f"{mid} is down: raised by an operator.",
                                          rul_hours=0, down_at=since)
                wo.update(status="awaiting")
                self.store.put("work_orders", wo)
                return wo
            rul = (m.rul or {}).get("hours") if m else stored.get("rul_hours")
            reason = (f"Scheduled from live monitoring: estimated {fmt_hours(rul)} to the limit." if rul is not None
                      else "Scheduled from live monitoring.")
            wo = self._new_work_order(mid, "planned", reason=reason, rul_hours=rul)
        return wo

    def take_offline(self, mid: str):
        with self.lock:
            wo = self.open_work_order(mid) or self._new_work_order(mid, "planned", reason="Taken offline for planned maintenance.")
            if self._engine_has(mid):
                self.engine.take_offline(mid)
            else:
                self._offline_state(mid, lifecycle="stopped", status="down", down_since=_now_iso(), work_order_id=wo["wo_id"])
            wo.update(status="awaiting", down_at=wo.get("down_at") or _now_iso())
            self.store.put("work_orders", wo)

    def start_repair(self, wo_id: str, technician: dict | None = None):
        with self.lock:
            wo = self.store.get("work_orders", wo_id)
            if not wo or wo["status"] not in ("planned", "awaiting"):
                return
            mid = wo["machine_id"]
            if self._engine_has(mid):
                self.engine.start_repair(mid)
                self.engine.set_work_order(mid, wo_id)
            else:
                self._offline_state(mid, lifecycle="in_repair", status="down", work_order_id=wo_id,
                                    down_since=(self.store.get("machine_state", mid) or {}).get("down_since") or _now_iso())
            now = _now_iso()
            wo.update(status="in_repair", started_at=now, down_at=wo.get("down_at") or now)
            if technician:
                wo.update(technician_id=technician.get("id"), technician_name=technician.get("name"))
            self.store.put("work_orders", wo)

    def complete_repair(self, wo_id: str, notes: str = "", parts_used: list | None = None, user: str | None = None):
        """parts_used: [{part_id, qty}] taken from the store; None issues what the order reserved."""
        with self.lock:
            wo = self.store.get("work_orders", wo_id)
            if not wo or wo["status"] != "in_repair":
                return
            mid = wo["machine_id"]
            used = self._parts(parts_ops.complete_for_wo, wo_id, parts_used, user)
            wo.update(status="commissioning", completed_at=_now_iso(), notes=notes or wo.get("notes", ""),
                      parts_used=used if used is not None else (parts_used or []))
            if self._engine_has(mid):
                self.store.put("work_orders", wo)
                self.engine.complete_repair(mid)
            else:
                # no live stream to recommission on: close now; the next session learns the new normal
                down = _epoch(wo.get("down_at"))
                wo.update(status="closed", closed_at=_now_iso(),
                          downtime_hours=round((time.time() - down) / 3600, 2) if down else None)
                self.store.put("work_orders", wo)
                rec = self.store.get("machine_state", mid) or {"machine_id": mid, "profile": {}}
                prof = dict(rec.get("profile") or {})
                prof["condition"] = {"kind": "healthy", "fault": None, "repaired_at": time.time()}
                self._offline_state(mid, lifecycle="running", status="learning", down_since=None, work_order_id=None,
                                    profile=prof, condition="healthy", fault_mode=None, fault_sensor=None)

    def cancel_work_order(self, wo_id: str):
        with self.lock:
            wo = self.store.get("work_orders", wo_id)
            if not wo or wo["status"] != "planned":
                return
            wo.update(status="cancelled", closed_at=_now_iso())
            self.store.put("work_orders", wo)
            self._parts(parts_ops.release_for_wo, wo_id, f"Work order {wo_id} cancelled")
            if self._engine_has(wo["machine_id"]):
                self.engine.set_work_order(wo["machine_id"], None)

    def _auto_complete(self, now: float):
        if not (self.context.get("automation") or {}).get("auto_complete_repair"):
            return
        for wo in self.work_orders():
            started = _epoch(wo.get("started_at"))
            if wo["status"] == "in_repair" and started and now - started >= float(wo.get("est_hours") or 0) * 3600:
                self.complete_repair(wo["wo_id"], notes="Completed automatically after the estimated repair time.")

    def reset_conditions(self) -> bool:
        """Forget every machine's stored condition (monitoring must be stopped); open work orders are cancelled."""
        with self.lock:
            if self.engine.active:
                return False
            for rec in self.store.all("machine_state"):
                self.store.delete("machine_state", rec["machine_id"])
            for wo in self.work_orders():
                if wo["status"] in OPEN_WO:
                    wo.update(status="cancelled", closed_at=_now_iso(), notes=(wo.get("notes") or "") + " Cancelled by a plant reset.")
                    self.store.put("work_orders", wo)
                    self._parts(parts_ops.release_for_wo, wo["wo_id"], "Plant reset")
            return True

    # -- reads -------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.engine.active

    def snapshot(self, reveal: bool = False) -> dict:
        with self.lock:
            s = self.engine.state(reveal=reveal)
            for k in ("machines", "alerts", "config", "scenario"):
                if k in s:
                    s[k] = copy.deepcopy(s[k])
            if "loss_curve" in s:
                s["loss_curve"] = list(s["loss_curve"])
            return s

    def series(self, machine_id: str, resolution: str) -> list[dict]:
        with self.lock:
            return self.engine.series(machine_id, resolution)

    def summary(self) -> dict:
        """Cheap status for the sidebar and header."""
        with self.lock:
            e = self.engine
            if not e.session_id:
                return {"active": False}
            statuses = ["down" if m.lifecycle in DOWN else m.status for m in e.machines.values()]
            return {"active": e.active, "paused": e.paused, "idle_paused": self.idle_paused, "session_id": e.session_id,
                    "tick": e.tick, "sim_time": e._sim_time(), "realtime": e.realtime, "started_at": e.started_at,
                    "phase": e.phase(), "training_progress": e.training_progress(),
                    "counts": {s: statuses.count(s) for s in ("critical", "warning", "drift", "healthy", "learning", "down")},
                    "critical": [m.id for m in e.machines.values() if m.status == "critical" and m.lifecycle not in DOWN],
                    "down": [m.id for m in e.machines.values() if m.lifecycle in DOWN]}

    def plant_kpis(self) -> dict:
        """Availability, downtime and the value of acting early, from live state and the work-order history."""
        wos = self.work_orders()
        with self.lock:
            machines = list(self.engine.machines.values()) if self.engine.session_id else []
            live = bool(machines) and self.engine.active
            down = [m.id for m in machines if m.lifecycle in DOWN]
        if not live:
            stored = self.store.all("machine_state")
            down = [r["machine_id"] for r in stored if r.get("lifecycle") in DOWN]
            total = len(stored)
        else:
            total = len(machines)
        planned = [w for w in wos if w["kind"] == "planned" and w["status"] != "cancelled"]
        unplanned = [w for w in wos if w["kind"] == "unplanned"]
        closed = [w for w in wos if w["status"] == "closed" and w.get("downtime_hours") is not None]
        leads = sorted(w["lead_hours"] for w in wos if w.get("lead_hours"))
        now = time.time()
        down_hours = sum(w["downtime_hours"] for w in closed if (_epoch(w.get("closed_at")) or 0) > now - 7 * 86400)
        down_hours += sum(max(now - _epoch(w["down_at"]), 0) / 3600 for w in wos
                          if w["status"] in ("awaiting", "in_repair") and _epoch(w.get("down_at")))
        return {"total": total, "down": down, "availability": round(100 * (total - len(down)) / total, 1) if total else None,
                "open": [w for w in wos if w["status"] in OPEN_WO], "planned": len(planned), "unplanned": len(unplanned),
                "avoided_cost_usd": round(sum(w.get("avoided_cost_usd") or 0 for w in planned)),
                "avoided_hours": round(sum(w.get("avoided_hours") or 0 for w in planned), 1),
                "lead_hours_median": leads[len(leads) // 2] if leads else None, "downtime_hours_7d": round(down_hours, 1)}

    def escalation_trigger(self, machine_id: str) -> dict:
        with self.lock:
            return copy.deepcopy(self.engine.escalation_trigger(machine_id))

    def outlook(self) -> dict[str, dict]:
        """Each monitored machine's state for planning (parts demand, business impact): live while monitoring
        runs, otherwise the last stored state with its time to failure counted down since it was saved."""
        now = time.time()
        rows: dict[str, dict] = {}
        with self.lock:
            if self.engine.active:
                for m in self.engine.machines.values():
                    down = m.lifecycle in DOWN
                    rows[m.id] = {"machine_id": m.id, "lifecycle": m.lifecycle, "status": "down" if down else m.status,
                                  "rul_hours": None if down else (m.rul or {}).get("hours"),
                                  "fault": (m.profile or {}).get("fault"), "down_since": m.down_since, "live": True}
        if not rows:
            for r in self.store.all("machine_state"):
                h, at = r.get("rul_hours"), _epoch(r.get("updated_at"))
                running = r.get("lifecycle") not in DOWN
                rows[r["machine_id"]] = {
                    "machine_id": r["machine_id"], "lifecycle": r.get("lifecycle"), "status": r.get("status") if running else "down",
                    "rul_hours": max(h - (now - at) / 3600, 0) if running and h is not None and at else None,
                    "fault": r.get("fault_mode"), "down_since": _epoch(r.get("down_since")), "live": False}
        for w in self.work_orders():
            if w["status"] in OPEN_WO and w["machine_id"] in rows and not rows[w["machine_id"]].get("work_order"):
                rows[w["machine_id"]].update(work_order=w, has_open_wo=True)
        return rows
