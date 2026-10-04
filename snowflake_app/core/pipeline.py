"""
8D multi-agent pipeline, framework-free.

Each node = one agent. A node (1) runs its deterministic tools against the
plant data, emitting tool calls/results, (2) asks the LLM to reason over that
evidence plus the upstream agents' conclusions. A conditional edge after the
predictive agent decides whether a full 8D investigation is warranted.

The React backend compiles these nodes into a LangGraph StateGraph; the
Streamlit app walks the same graph with run_pipeline(). Both pass in their own
`complete(system, prompt, on_token) -> LLMResult` callable.
"""

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, TypedDict

from snowflake_app.core import data as D
from snowflake_app.core.monitoring import fmt_hours
from snowflake_app.tools.prediction_engine import LIVE_THRESHOLDS as SENSOR_THRESHOLDS

STEPS = [
    {"id": "sensor_scan", "code": "SCAN", "title": "Sensor Monitor", "subtitle": "Fleet signal triage"},
    {"id": "predictive", "code": "PdM", "title": "Predictive Maintenance", "subtitle": "RUL and maintenance decision"},
    {"id": "d0", "code": "D0", "title": "Prepare & Trigger", "subtitle": "Severity, scope, emergency response"},
    {"id": "d1", "code": "D1", "title": "Team Formation", "subtitle": "Cross-functional team"},
    {"id": "d2", "code": "D2", "title": "Problem Definition", "subtitle": "5W2H and IS / IS NOT"},
    {"id": "d3", "code": "D3", "title": "Containment", "subtitle": "Lot trace and quarantine"},
    {"id": "d4", "code": "D4", "title": "Root Cause", "subtitle": "6M, 5-Why, SPC evidence"},
    {"id": "d5", "code": "D5", "title": "Corrective Actions", "subtitle": "Permanent fixes"},
    {"id": "d6", "code": "D6", "title": "Implementation", "subtitle": "Plan and validation"},
    {"id": "d7", "code": "D7", "title": "Prevention", "subtitle": "Systemic and horizontal deployment"},
    {"id": "d8", "code": "D8", "title": "Closure", "subtitle": "Summary, COPQ, recognition"},
]
STEP_IDS = [s["id"] for s in STEPS]

GROUNDING = (
    "You are part of a multi-agent 8D problem-solving system in a discrete manufacturing plant. "
    "Respond in GitHub-flavored Markdown using exactly the section headings requested (### level). "
    "Ground every statement in the EVIDENCE provided: cite machine IDs, sensor values, counts, lot IDs, "
    "people and costs from it. Never invent measurements; if information is missing, say which data is needed. "
    "Prefer compact tables where asked. Keep the whole answer under 320 words. No preamble, no closing remarks."
)

AGENTS = {
    "sensor_scan": ("Sensor Monitor Agent: triage fleet sensor signals and identify machines at risk.",
                    ["Signal summary", "Machines at risk (table: machine | sensor | current | critical limit | risk)", "Assessment"]),
    "predictive": ("Predictive Maintenance Agent: interpret degradation trends, remaining useful life, maintenance history and spare-part readiness.",
                   ["Degradation analysis", "Remaining useful life", "Maintenance history & spares", "Decision (MONITOR / SCHEDULE MAINTENANCE / TRIGGER 8D, with reason)"]),
    "d0": ("D0 Agent: classify severity, estimate scope and define emergency response actions.",
           ["Severity classification", "Scope", "Emergency response actions", "8D decision"]),
    "d1": ("D1 Agent: form a cross-functional team using the named people in the evidence where possible.",
           ["Team charter", "Team members (table: role | person | reason)", "Champion & meeting cadence"]),
    "d2": ("D2 Agent: define the problem precisely with 5W2H and IS / IS NOT, quantified from the evidence.",
           ["Problem statement (5W2H)", "IS / IS NOT (table: dimension | IS | IS NOT)", "Quantified impact"]),
    "d3": ("D3 Agent: contain the problem using the lot traceability results.",
           ["Affected scope", "Containment actions (table: action | owner role | due)", "Customer notification", "Effectiveness check"]),
    "d4": ("D4 Agent: determine and verify root cause with the 6M framework, 5-Why and SPC evidence. State confidence levels.",
           ["6M analysis (table: factor | evidence | verdict)", "5-Why", "Verified root cause(s) with confidence", "Escape point"]),
    "d5": ("D5 Agent: choose permanent corrective actions for occurrence and detection.",
           ["Corrective actions (table: action | addresses | effectiveness | cost | risk)", "Selected actions & rationale"]),
    "d6": ("D6 Agent: plan implementation and define how effectiveness will be validated.",
           ["Implementation plan (table: task | owner role | start | due)", "Validation criteria", "Documents to update"]),
    "d7": ("D7 Agent: prevent recurrence systemically and deploy horizontally to similar equipment.",
           ["Horizontal deployment", "Systemic changes", "Lessons learned"]),
    "d8": ("D8 Agent: close the 8D with an executive summary, cost of poor quality and recognition.",
           ["Executive summary", "Cost of poor quality", "Closure checklist", "Recognition"]),
}


class Cancelled(Exception):
    pass


class State(TypedDict, total=False):
    trigger: dict
    machine_id: str
    evidence: dict
    outputs: dict
    escalate: bool


# ---------------------------------------------------------------------------
# Tools per step: (name, label, fn) -> result dict + one-line summary
# ---------------------------------------------------------------------------

def tools_for(step: str, state: State) -> list[tuple[str, str, Callable[[], tuple[dict, str]]]]:
    mid = state["machine_id"]
    ev = state.get("evidence", {})
    trig = state.get("trigger", {})

    def machine_defects():
        d = D.defects_enriched()
        return d[d["machine_id"] == mid]

    def live_signal() -> dict | None:
        """The live model's view of the triggering sensor, with its alarm limits."""
        live = trig.get("live_snapshot")
        if not live:
            return None
        sensor, value = trig.get("sensor"), trig.get("value")
        lim = SENSOR_THRESHOLDS.get(sensor, {})
        upper = lim.get("direction") == "upper"
        crit = lim.get("critical")
        breached = value is not None and crit is not None and (value >= crit if upper else value <= crit)
        return {"sensor": sensor, "value": value, "warning_limit": lim.get("warning"), "critical_limit": crit,
                "limit_direction": "max" if upper else "min", "critical_limit_breached": breached,
                "model_status": live.get("status"), "health_pct": live.get("health"), "anomaly_score": live.get("anomaly_score"),
                "drift": live.get("drift"), "rul_hours": trig.get("rul_hours"),
                "time_to_critical": fmt_hours(trig.get("rul_hours")) if trig.get("rul_hours") is not None else None,
                "observed_at": next((e.get("sim_time") for e in reversed(live.get("recent_events") or []) if e.get("sim_time")), None),
                "erp_process_baseline": erp_baseline()}

    def erp_baseline() -> dict | None:
        """What the ERP process records say this machine normally runs at, to set the live reading against."""
        p = D.table("process_records")
        p = p[p["machine_id"] == mid]
        if p.empty:
            return None
        out = {"orders": int(p["order_id"].nunique())}
        for col, key in (("speed_rpm", "spindle_rpm"), ("temperature_c", "temp_c"), ("pressure_bar", "pressure_bar")):
            if col in p:
                s = p[col].astype(float)
                out[key] = {"median": round(float(s.median()), 1), "p05": round(float(s.quantile(0.05)), 1),
                            "p95": round(float(s.quantile(0.95)), 1)}
        return out

    def scan():
        alerts = D.historical_alerts()
        live = trig.get("live_snapshot")
        sig = live_signal()
        res = {"historical_alerts": alerts[:8], "live": live, "live_signal": sig}
        s = f"{len(alerts)} historical alert(s)"
        if sig:
            s += (f"; live {sig['model_status']} on {trig.get('machine_id')}: {sig['sensor']} {sig['value']} "
                  f"vs critical {sig['limit_direction']} {sig['critical_limit']}")
        return res, s

    def health():
        rows = D.sensor_health(mid)
        worst = [r for r in rows if r.get("risk_level") in ("critical", "high")]
        s = f"{len(rows)} sensors analysed from history; {len(worst)} at high/critical risk" if rows else "No sensor telemetry for this machine"
        res = {"machine": D.machine_profile(mid), "sensors": rows}
        sig = live_signal()
        if sig:
            res["live_signal"] = sig
            rul = sig["rul_hours"]
            s += f"; live model {sig['model_status']}, RUL " + ("0 h (limit crossed)" if sig["critical_limit_breached"] else
                                                              fmt_hours(rul) if rul is not None else "n/a")
        return res, s

    def maint():
        m = D.maintenance_history(mid)
        low = [p["part_name"] for p in m["spare_parts"] if p["qty_on_hand"] < p["reorder_point"]]
        return m, f"{m['total_events']} maintenance events, ${m['total_cost_usd']:,.0f}; below reorder: {', '.join(low) or 'none'}"

    def breakdown():
        b = D.defect_breakdown(mid)
        return b, (f"{b['machine_defects']} defects ({b['share_of_plant_pct']}% of plant); fail rate "
                   f"{b['machine_fail_rate_pct']}% vs {b['rest_of_plant_fail_rate_pct']}% rest of plant")

    def copq_prelim():
        c = D.estimate_copq_for(machine_defects())
        return c, f"Preliminary COPQ ${c['total_copq']:,.0f}"

    def team():
        t = D.team_candidates(mid)
        return {"operators": t}, f"{len(t)} operators with recent runs on {mid}"

    def trace():
        ids = ev.get("defect_breakdown", {}).get("defect_ids") or D.defect_breakdown(mid)["defect_ids"]
        if not ids:
            return {"error": "no defects on machine"}, "No defects to trace"
        t = D.traceability(ids[0])
        imp = t.get("impact_summary", {})
        lots = [l.get("lot_id") for l in t.get("backward_trace", {}).get("material_lots", [])]
        res = {"defect_id": ids[0], "material_lots": lots, "impact": {
            k: imp.get(k) for k in ("total_orders_affected", "total_shipments_affected", "total_customers_affected", "total_units_at_risk")},
            "customers": imp.get("containment_actions_needed", {}).get("notify_customers", []),
            "orders_sample": imp.get("containment_actions_needed", {}).get("quarantine_orders", [])[:12]}
        i = res["impact"]
        return res, (f"{ids[0]} -> lots {', '.join(lots)} -> {i['total_orders_affected']} orders, "
                     f"{i['total_shipments_affected']} shipments, {i['total_customers_affected']} customers, "
                     f"{i['total_units_at_risk']:,} units")

    def spc():
        s = D.spc_for_machine(mid)
        return s, f"{s['characteristic']} on {s['scope']}: Cpk {s['cpk']}, ~{s['ppm']:,.0f} PPM, {len(s['violations'])} Nelson violation(s)"

    def suppliers():
        rows = D.supplier_exposure(mid)
        fails = [r for r in rows if r["coa_status"] != "PASS"]
        return {"lots": rows}, f"{len(rows)} material lots on defective orders; {len(fails)} with CoA issues"

    def peers():
        p = D.similar_machines(mid)
        return {"similar_machines": p}, f"{len(p)} machines of the same type for horizontal deployment"

    def copq_final():
        units = ev.get("trace", {}).get("impact", {}).get("total_units_at_risk") or 0
        c = D.estimate_copq_for(machine_defects(), units)
        return c, f"Total COPQ ${c['total_copq']:,.0f} (internal {c['breakdown_pct']['internal']}%, external {c['breakdown_pct']['external']}%)"

    return {
        "sensor_scan": [("scan_fleet", "Scan fleet sensors", scan)],
        "predictive": [("sensor_health", f"Estimate RUL for {mid}", health), ("maintenance_history", f"Load maintenance & spares for {mid}", maint)],
        "d0": [("defect_breakdown", f"Profile defects on {mid}", breakdown), ("copq_preliminary", "Preliminary cost of poor quality", copq_prelim)],
        "d1": [("team_candidates", f"Find operators on {mid}", team)],
        "d2": [("defect_breakdown", "IS / IS NOT comparison vs plant", breakdown)],
        "d3": [("trace", "Bidirectional lot trace", trace)],
        "d4": [("spc", "SPC capability & Nelson rules", spc), ("supplier_exposure", "Material lot & supplier exposure", suppliers)],
        "d5": [("maintenance_history", "Spare parts & maintenance cost", maint)],
        "d6": [],
        "d7": [("similar_machines", "Find similar equipment", peers)],
        "d8": [("copq", "Final cost of poor quality", copq_final)],
    }[step]


def decide_escalation(state: State) -> bool:
    ev = state["evidence"]
    trig = state.get("trigger", {})
    sensors = ev.get("sensor_health", {}).get("sensors", [])
    if trig.get("risk") in ("critical", "high") or any(s.get("trigger_8d") for s in sensors):
        return True
    b = D.defect_breakdown(state["machine_id"])
    return b["machine_defects"] >= 3 and b["machine_fail_rate_pct"] > 2 * max(b["rest_of_plant_fail_rate_pct"], 0.1)


def _compact(obj, limit=3500) -> str:
    s = json.dumps(obj, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "…(truncated)"


def build_prompt(step: str, state: State, step_evidence: dict) -> tuple[str, str]:
    role, sections = AGENTS[step]
    system = f"{GROUNDING}\n\nROLE: {role}"
    trig = state.get("trigger", {})
    parts = [
        f"TRIGGER: {_compact({k: v for k, v in trig.items() if k != 'live_snapshot'}, 600)}",
        f"MACHINE: {_compact(D.machine_profile(state['machine_id']), 400)}",
        f"EVIDENCE:\n{_compact(step_evidence)}",
    ]
    prior = [(sid, state["outputs"][sid]) for sid in STEP_IDS if sid in state.get("outputs", {})]
    if prior:
        parts.append("UPSTREAM AGENT CONCLUSIONS:\n" + "\n\n".join(
            f"[{sid}]\n{text[:900]}" for sid, text in prior[-4:]))
    parts.append("Write these sections:\n" + "\n".join(f"### {s}" for s in sections))
    return system, "\n\n".join(parts)


def run_node(step: str, state: State, complete, emit, cancel: threading.Event) -> dict:
    """Execute one agent: tools first, then LLM reasoning. Returns the state update."""
    if cancel.is_set():
        raise Cancelled()
    emit({"type": "step_started", "step": step})
    evidence = dict(state.get("evidence", {}))
    step_evidence = {}
    for name, label, fn in tools_for(step, state):
        emit({"type": "tool_started", "step": step, "tool": name, "label": label})
        t0 = time.time()
        result, summary = fn()
        evidence[name] = result
        step_evidence[name] = result
        emit({"type": "tool_finished", "step": step, "tool": name, "label": label, "summary": summary,
              "duration_s": round(time.time() - t0, 2), "result": result})
    if step == "sensor_scan" and not step_evidence:
        step_evidence = {"note": "no tools"}
    if step == "d6":
        step_evidence = {"root_cause_and_actions": "see upstream conclusions", "team": evidence.get("team_candidates")}

    system, prompt = build_prompt(step, {**state, "evidence": evidence}, step_evidence)
    emit({"type": "llm_started", "step": step})
    res = complete(system, prompt, on_token=lambda tok: emit({"type": "token", "step": step, "text": tok}))
    if cancel.is_set():
        raise Cancelled()
    emit({"type": "step_finished", "step": step, "output": res.text, "latency_s": res.latency_s,
          "usage": res.usage, "model": res.model, "provider": res.provider})
    update = {"evidence": evidence, "outputs": {**state.get("outputs", {}), step: res.text}}
    if step == "predictive":
        update["escalate"] = decide_escalation({**state, **update})
        emit({"type": "decision", "step": step, "escalate": update["escalate"],
              "message": "Escalating to full 8D investigation" if update["escalate"]
              else "Risk below 8D threshold: monitoring recommended, pipeline ends"})
    return update


def run_pipeline(trigger: dict, complete, emit, cancel: threading.Event) -> State:
    """Walk the graph: SCAN -> PdM -> (escalate?) -> D0 ... D8. Same topology as the LangGraph build."""
    state: State = {"trigger": trigger, "machine_id": trigger["machine_id"], "evidence": {}, "outputs": {}}
    for sid in STEP_IDS:
        state = {**state, **run_node(sid, state, complete, emit, cancel)}
        if sid == "predictive" and not state.get("escalate"):
            break
    return state


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def resolve_trigger(trigger: dict) -> dict:
    """Fill in machine/sensor details. 'auto' picks the most severe historical sensor alert."""
    trig = dict(trigger or {})
    trig.setdefault("source", "auto")
    if not trig.get("machine_id"):
        alerts = D.historical_alerts()
        if alerts:
            a = alerts[0]
            trig.update(machine_id=a["machine_id"], sensor=a["sensor"], value=a["current_value"], risk=a["risk_level"])
        else:
            trig["machine_id"] = D.defects_enriched()["machine_id"].value_counts().index[0]
    if trig.get("source") in ("auto", "machine") and not trig.get("risk"):
        rows = D.sensor_health(trig["machine_id"])
        worst = sorted(rows, key=lambda r: {"critical": 0, "high": 1, "medium": 2}.get(r.get("risk_level"), 3))
        if worst:
            trig.setdefault("sensor", worst[0]["sensor"])
            trig.setdefault("value", worst[0]["current_value"])
            trig["risk"] = worst[0]["risk_level"]
    m = D.machine_profile(trig["machine_id"])
    trig["machine_name"] = m.get("name")
    trig["line"] = m.get("line")
    return trig


def trigger_options(live_alerts: list[dict]) -> list[dict]:
    """Candidate investigations: live monitoring alerts, historical sensor alerts, defect clusters."""
    out = []
    for a in live_alerts:
        out.append({"source": "live", "machine_id": a["machine_id"], "label": f"Live alert · {a['machine_id']} {a['name']}",
                    "detail": f"{a['sensor']} = {a['value']} at {str(a['sim_time']).replace('T', ' ')[:19]}", "risk": "critical"})
    for a in D.historical_alerts()[:3]:
        out.append({"source": "auto", "machine_id": a["machine_id"], "label": f"Sensor alert · {a['machine_id']}",
                    "detail": f"{a['sensor']} = {a['current_value']} ({a['risk_level']}), RUL {a.get('rul_days')} d",
                    "risk": a["risk_level"]})
    bd = D.defects_enriched()["machine_id"].value_counts()
    for mid, n in bd.head(3).items():
        if not any(t["machine_id"] == mid for t in out):
            out.append({"source": "machine", "machine_id": mid, "label": f"Defect cluster · {mid}",
                        "detail": f"{n} defects recorded", "risk": "high" if n >= 5 else "medium"})
    return out


# ---------------------------------------------------------------------------
# Run record: event reducer shared by both apps
# ---------------------------------------------------------------------------

def new_steps() -> dict:
    return {s["id"]: {**s, "status": "pending", "tools": [], "output": "", "started_at": None,
                      "finished_at": None, "latency_s": None, "usage": {}} for s in STEPS}


class RunRecord:
    """State of one investigation, updated from pipeline events (thread-safe)."""

    def __init__(self, trigger: dict, provider: dict):
        self.id = f"8D-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.trigger = trigger
        self.provider = provider
        self.status = "queued"
        self.error = None
        self.steps = new_steps()
        self.decision = None
        self.events: list[dict] = []
        self.finished_at = None
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    def snapshot(self, with_events: bool = False) -> dict:
        with self._lock:
            tokens = sum((s["usage"] or {}).get("total_tokens") or 0 for s in self.steps.values())
            done = sum(1 for s in self.steps.values() if s["status"] in ("done", "skipped"))
            snap = {
                "id": self.id, "created_at": self.created_at, "finished_at": self.finished_at,
                "status": self.status, "error": self.error, "trigger": self.trigger, "provider": self.provider,
                "decision": self.decision, "steps": [dict(self.steps[i], tools=list(self.steps[i]["tools"])) for i in STEP_IDS],
                "progress": {"done": done, "total": len(STEP_IDS)}, "total_tokens": tokens,
            }
            if with_events:
                snap["events"] = list(self.events)
        return snap

    def apply(self, ev: dict) -> dict:
        """Update run state from an event. Returns the timestamped event."""
        ev = {**ev, "ts": time.time()}
        with self._lock:
            step = self.steps.get(ev.get("step")) if ev.get("step") else None
            t = ev["type"]
            if t == "step_started":
                step.update(status="running", started_at=ev["ts"])
            elif t == "tool_started":
                step["tools"].append({"tool": ev["tool"], "label": ev["label"], "status": "running"})
            elif t == "tool_finished":
                for tool in reversed(step["tools"]):
                    if tool["tool"] == ev["tool"] and tool["status"] == "running":
                        tool.update(status="done", summary=ev["summary"], duration_s=ev["duration_s"], result=ev["result"])
                        break
            elif t == "llm_started":
                step.update(status="thinking", llm_started_at=ev["ts"])
            elif t == "token":
                step["output"] += ev["text"]
            elif t == "step_finished":
                step.update(status="done", output=ev["output"], finished_at=ev["ts"], latency_s=ev["latency_s"],
                            usage=ev.get("usage") or {}, model=ev.get("model"))
            elif t == "decision":
                self.decision = {"escalate": ev["escalate"], "message": ev["message"]}
            elif t == "step_failed":
                step.update(status="failed", error=ev["error"], finished_at=ev["ts"])
            if t != "token":
                self.events.append({k: v for k, v in ev.items() if k != "result"})
        return ev

    def mark_skipped(self):
        with self._lock:
            for s in self.steps.values():
                if s["status"] == "pending":
                    s["status"] = "skipped"


def report_markdown(snap: dict) -> str:
    t = snap["trigger"]
    lines = [
        f"# 8D Report {snap['id']}",
        "",
        "| Field | Value |", "|---|---|",
        f"| Created | {snap['created_at']} |",
        f"| Status | {snap['status']} |",
        f"| Machine | {t.get('machine_id')} {t.get('machine_name') or ''} ({t.get('line') or ''}) |",
        f"| Trigger | {t.get('source')} |",
        f"| Signal | {t.get('sensor') or '-'} = {t.get('value') or '-'} ({t.get('risk') or '-'}) |",
        f"| AI provider | {snap['provider'].get('label')} / {snap['provider'].get('model')} |",
        "",
    ]
    for s in snap["steps"]:
        if s["status"] != "done":
            continue
        lines += [f"## {s['code']} — {s['title']}", ""]
        for tool in s.get("tools", []):
            if tool.get("summary"):
                lines.append(f"> Evidence · {tool['label']}: {tool['summary']}")
        if s.get("tools"):
            lines.append("")
        lines += [s["output"], ""]
    return "\n".join(lines)
