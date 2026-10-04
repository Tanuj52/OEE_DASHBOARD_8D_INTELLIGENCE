"""
8D run manager for the Streamlit app: runs the shared pipeline in a worker
thread and keeps live runs in memory. Finished runs are saved to the
operations store (OPERATIONS.INVESTIGATIONS in Snowflake), so the History and
Reports pages survive restarts and redeploys, and every viewer sees the same
reports. Runs saved as JSON by earlier versions (snowflake_app/.runs) are
imported once.

One investigation runs at a time. Automation (a machine trip) enqueues runs:
they start when the current one finishes, in arrival order. on_complete is
called after every finished run with the run snapshot and the context it was
started with (used to email reports automatically).
"""

import json
import os
import threading
import uuid
from datetime import datetime

from snowflake_app.core.pipeline import STEP_IDS, Cancelled, RunRecord, new_steps, resolve_trigger, run_pipeline
from snowflake_app.services import ops_store
from snowflake_app.services.llm import ProviderNotConfigured

RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".runs")
SUMMARY = ("id", "created_at", "finished_at", "status", "trigger", "provider", "progress", "decision", "total_tokens")


def _record(snap: dict) -> dict:
    trig = snap.get("trigger") or {}
    return dict(snap, machine_id=trig.get("machine_id"), source=trig.get("source"))


class RunManager:
    def __init__(self):
        self.runs: dict[str, RunRecord] = {}  # this process's runs; finished ones also live in the store
        self._lock = threading.Lock()
        self.queue: list[dict] = []
        self.on_complete = None  # callable(snapshot, context)
        self._context: dict[str, dict] = {}
        self._imported = set()

    @staticmethod
    def store():
        return ops_store.store()

    def import_legacy(self):
        """Copy runs saved as local JSON by earlier versions into the store (once per store backend)."""
        st = self.store()
        key = st.config_key
        if key in self._imported or not os.path.isdir(RUNS_DIR):
            return
        self._imported.add(key)
        for f in sorted(os.listdir(RUNS_DIR)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(RUNS_DIR, f)) as fh:
                    snap = json.load(fh)
                if snap.get("id") and not st.get("investigations", snap["id"]):
                    st.put("investigations", _record(snap))
            except (OSError, json.JSONDecodeError):
                continue

    def active(self) -> RunRecord | None:
        return next((r for r in list(self.runs.values()) if r.status in ("queued", "running")), None)

    def start(self, trigger: dict, complete, provider: dict, context: dict | None = None) -> RunRecord:
        with self._lock:
            if self.active():
                raise RuntimeError("An investigation is already running. Cancel it or wait for it to finish.")
            run = self._create(trigger, provider, context)
        self._launch(run, complete)
        return run

    def _create(self, trigger: dict, provider: dict, context: dict | None) -> RunRecord:
        """Caller holds _lock."""
        run = RunRecord(resolve_trigger(trigger), provider)
        self.runs[run.id] = run
        self._context[run.id] = context or {}
        return run

    def _launch(self, run: RunRecord, complete):
        threading.Thread(target=self._execute, args=(run, complete), daemon=True, name=run.id).start()

    def enqueue(self, trigger: dict, complete, provider: dict, context: dict | None = None, label: str = "") -> dict:
        """Start now if idle, otherwise queue. Returns {"run_id"} or {"queued_id", "position"}.

        The busy check and the run creation happen under one lock, so machines tripping together
        (e.g. several found stopped when monitoring restarts) queue instead of racing."""
        with self._lock:
            if self.active() is None and not self.queue:
                run = self._create(trigger, provider, context)
            else:
                item = {"id": f"Q-{uuid.uuid4().hex[:6].upper()}", "trigger": trigger, "complete": complete,
                        "provider": provider, "context": context or {}, "label": label or trigger.get("machine_id") or "",
                        "queued_at": datetime.now().isoformat(timespec="seconds")}
                self.queue.append(item)
                return {"queued_id": item["id"], "position": len(self.queue)}
        self._launch(run, complete)
        return {"run_id": run.id}

    def queued(self) -> list[dict]:
        with self._lock:
            return [{k: q[k] for k in ("id", "label", "queued_at")} | {"machine_id": q["trigger"].get("machine_id"),
                     "reason": q["context"].get("reason")} for q in self.queue]

    def cancel_queued(self, qid: str) -> bool:
        with self._lock:
            n = len(self.queue)
            self.queue = [q for q in self.queue if q["id"] != qid]
            return len(self.queue) < n

    def _next(self):
        with self._lock:
            item = self.queue.pop(0) if self.queue and not self.active() else None
        if not item:
            return
        try:
            run = self.start(item["trigger"], item["complete"], item["provider"], item["context"])
            cb = item["context"].get("on_start")
            if cb:
                cb(run.id)
        except Exception as e:
            # keep a record instead of dropping the investigation silently
            now = datetime.now().isoformat(timespec="seconds")
            rid = f"8D-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"
            self.store().put("investigations", _record({
                "id": rid, "created_at": item["queued_at"], "finished_at": now, "status": "failed",
                "error": f"Could not start the queued investigation: {str(e).splitlines()[0][:300]}",
                "trigger": item["trigger"], "provider": item["provider"], "decision": None,
                "steps": [new_steps()[i] for i in STEP_IDS],
                "progress": 0, "total_tokens": 0}))
            self._next()

    def _execute(self, run: RunRecord, complete):
        run.status = "running"
        run.apply({"type": "run_started", "run_id": run.id, "trigger": run.trigger, "provider": run.provider})
        current = {"step": None}

        def emit(ev):
            if ev["type"] == "step_started":
                current["step"] = ev["step"]
            run.apply(ev)

        try:
            if not run.provider["configured"]:
                raise ProviderNotConfigured(f"{run.provider['label']} is not configured. Open Settings to add credentials.")
            run_pipeline(run.trigger, complete, emit, run.cancel)
            run.mark_skipped()
            run.status = "completed"
        except Cancelled:
            run.status = "cancelled"
        except Exception as e:
            run.status = "failed"
            run.error = str(e).split("\n")[0][:500]
            if current["step"]:
                run.apply({"type": "step_failed", "step": current["step"], "error": run.error})
        finally:
            run.finished_at = datetime.now().isoformat(timespec="seconds")
            run.apply({"type": "run_finished", "status": run.status, "error": run.error})
            self.store().put("investigations", _record(run.snapshot()))
            ctx = self._context.pop(run.id, {})
            if self.on_complete:
                try:
                    self.on_complete(run.snapshot(), ctx)
                except Exception:
                    pass  # automation must never break the run manager
            self._next()

    def get(self, run_id: str) -> dict | None:
        r = self.runs.get(run_id)
        if r is not None:
            return r.snapshot()
        return self.store().get("investigations", run_id)

    def list(self) -> list[dict]:
        self.import_legacy()
        seen = {}
        for s in self.store().all("investigations"):
            seen[s["id"]] = {k: s.get(k) for k in SUMMARY}
        for r in list(self.runs.values()):
            s = r.snapshot()
            seen[s["id"]] = {k: s.get(k) for k in SUMMARY}
        return sorted(seen.values(), key=lambda x: x["created_at"] or "", reverse=True)

    def cancel(self, run_id: str) -> bool:
        r = self.runs.get(run_id)
        if r is not None and r.status in ("queued", "running"):
            r.cancel.set()
            return True
        return False
