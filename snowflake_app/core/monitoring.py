"""
Live monitoring engine.

Streams telemetry for every sensor-equipped machine and runs a real online
model over it:

  1. Learning   - per-machine baseline (Welford mean/variance) with a
                  prequential (predict-then-update) NLL loss curve.
  2. Scoring    - multivariate Gaussian (shrunk covariance) -> Mahalanobis
                  distance vs. a Hotelling T² limit; per-sensor attribution.
  3. Drift      - Population Stability Index of a recent window vs. the
                  training reference, per sensor.
  4. Classify   - drift with a significant monotonic trend = degradation
                  (retraining suppressed); stable step shift = benign process
                  change (model re-learns the new normal).
  5. Forecast   - linear fit on the degrading sensor -> remaining useful life
                  with a confidence interval and a forecast cone.

Two clocks:
  simulated - every sample advances a virtual clock by minutes_per_tick and the
              caller sets the pace (speed). One machine develops one fault over
              ~100 samples. Used by the React app.
  realtime  - samples carry the wall-clock time they are produced, one every
              sample_interval_s, and the runner calls advance_to() so the stream
              stays in sync with the real clock. Used by Streamlit.

On the real clock the plant behaves like a plant:
  - Each machine has a persisted condition: healthy, or a fault that started at
    a point in time and reaches the critical limit at another (hours to days
    later). Wear is a function of wall-clock time, so stopping and restarting the
    stream, or coming back tomorrow, continues the same timeline.
  - The baseline is learned from the machine's normal period (after its last
    service), so a machine that is already wearing looks worn from the first
    sample. The last 7 days are reconstructed at 10-minute and 1-minute
    resolution, and live samples roll into those series; remaining useful life
    is fitted on them, so it reads in hours and days and counts down in real
    time.
  - Lifecycle: a machine that stays above a critical limit trips (protective
    stop) and stops streaming. It stays down until maintenance is started and
    completed; it then recommissions (re-learns its new normal with alerts
    muted) and returns to service with a new model version.

The engine is synchronous and framework-free: callers drive it with step() or
advance_to() and observe it through on_event / on_state. Telemetry stays in
memory (bounded deques); persistence of state is the caller's job.
"""

import math
import random
import time
import uuid
from collections import deque
from datetime import datetime, timedelta

import numpy as np
from scipy import stats

from snowflake_app.tools.prediction_engine import LIVE_THRESHOLDS as SENSOR_THRESHOLDS
from snowflake_app.core import data as D

SENSORS = ["vibration_mm_s", "temp_c", "spindle_rpm", "pressure_bar", "power_kw", "coolant_flow_lpm"]
SENSOR_LABEL = {"vibration_mm_s": "Vibration", "temp_c": "Temperature", "spindle_rpm": "Spindle speed", "pressure_bar": "Pressure",
                "power_kw": "Power", "coolant_flow_lpm": "Coolant flow"}
SENSOR_UNIT = {"vibration_mm_s": "mm/s", "temp_c": "°C", "spindle_rpm": "rpm", "pressure_bar": "bar", "power_kw": "kW",
               "coolant_flow_lpm": "L/min"}
BASE = {"vibration_mm_s": (1.8, 0.15), "temp_c": (42.0, 1.2), "spindle_rpm": (2500.0, 12.0), "pressure_bar": (6.2, 0.2),
        "power_kw": (12.5, 0.6), "coolant_flow_lpm": (4.5, 0.15)}

FAULTS = {
    "bearing_wear": {"label": "Bearing wear", "sensor": "vibration_mm_s", "couple": {"temp_c": 0.45}},
    "thermal_runaway": {"label": "Spindle thermal runaway", "sensor": "temp_c", "couple": {"power_kw": 0.4}},
    "hydraulic_leak": {"label": "Hydraulic pressure leak", "sensor": "pressure_bar", "couple": {"power_kw": 0.35}},
    "motor_overload": {"label": "Drive motor overload", "sensor": "power_kw", "couple": {"temp_c": 0.5}},
    "coolant_blockage": {"label": "Coolant line blockage", "sensor": "coolant_flow_lpm", "couple": {"temp_c": 0.55}},
    "belt_slip": {"label": "Drive belt slip", "sensor": "spindle_rpm", "couple": {"vibration_mm_s": 0.4}},
}
SENSITIVITY_ALPHA = {"high": 1e-2, "medium": 1e-3, "low": 1e-4}
HISTORY = 240
TRANSITION = 8        # samples skipped after a drift opens (step changes settle here)
CLASSIFY_AFTER = 30   # samples after drift opens before the trend test runs
PSI_BINS = 5
DRIFT_ALPHA = 1e-4    # per-test false-alarm rate for the calibrated PSI critical value
RECUR_WINDOW = 90     # same-direction drift re-opening within this many samples of a retrain = creeping fault
TIERS = {"plan": 72.0, "urgent": 24.0, "critical": 4.0}   # remaining-useful-life action tiers, hours
MAX_CATCHUP = 120         # realtime: replay at most this many missed samples, beyond that record a gap
MID_S, MID_KEEP = 60, 24 * 60           # 1-minute aggregates, last 24 h
LONG_S, LONG_KEEP = 600, 7 * 24 * 6     # 10-minute aggregates, last 7 days
WARMUP = 30                              # realtime: live samples replayed before "now" so charts open populated
DOWN = ("tripped", "stopped", "in_repair")
LIFECYCLE_LABEL = {"running": "Running", "commissioning": "Commissioning", "tripped": "Down · awaiting maintenance",
                   "stopped": "Stopped for maintenance", "in_repair": "In repair"}

# Real-clock fleet mix: (remaining life at assignment, total life from fault onset, curve steepness k)
CONDITIONS = {
    "near_failure": {"remaining_h": (20 / 60, 40 / 60), "life_h": (2.5, 4.0), "k": 3.0},
    "plan": {"remaining_h": (8, 40), "life_h": (72, 144), "k": 1.2},
    "wear": {"remaining_h": (72, 240), "life_h": (216, 480), "k": 0.8},
}


def fmt_hours(h) -> str:
    """Human duration for a value in hours (seconds, minutes, hours or days)."""
    if h is None:
        return "n/a"
    h = float(h)
    if h * 60 < 1.5:
        return f"{h * 3600:.0f} s"
    if h < 1.5:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.1f} d"


def hotelling_threshold(n: int, p: int, alpha: float) -> float:
    """Distance threshold for a new observation when mean/cov are estimated from n samples."""
    t2 = p * (n + 1) * (n - 1) / (n * (n - p)) * stats.f.ppf(1 - alpha, p, n - p)
    return float(math.sqrt(t2))


def psi_critical(n_ref: int, n_cur: int, bins: int = PSI_BINS, alpha: float = DRIFT_ALPHA) -> float:
    """Under no drift PSI ~ (1/n_ref + 1/n_cur) * chi2(bins-1); small windows need a higher bar than 0.25."""
    return float((1 / n_ref + 1 / n_cur) * stats.chi2.ppf(1 - alpha, bins - 1))


def _psi(ref: np.ndarray, cur: np.ndarray, bins: int = PSI_BINS) -> float:
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    r = (np.histogram(ref, edges)[0] + 0.5) / (len(ref) + 0.5 * (len(edges) - 1))
    c = (np.histogram(cur, edges)[0] + 0.5) / (len(cur) + 0.5 * (len(edges) - 1))
    return float(np.sum((c - r) * np.log(c / r)))


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds") if epoch else None


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


def wear_fraction(profile: dict | None, t: float) -> float:
    """Share of the distance from normal to the critical limit covered at time t (1.0 = at the limit)."""
    if not profile or not profile.get("fault"):
        return 0.0
    start, fail, k = profile["start_ts"], profile["fail_ts"], profile.get("k", 1.0)
    if t <= start:
        return 0.0
    u = min((t - start) / max(fail - start, 1.0), 1.25)
    return (math.exp(k * u) - 1) / (math.exp(k) - 1)


class MachineModel:
    def __init__(self, machine: dict, rng: random.Random, stored: dict | None = None):
        self.id = machine["machine_id"]
        self.name = machine["name"]
        self.line = machine["line"]
        self.type = machine.get("type")
        prof = (stored or {}).get("profile") or {}
        # Each machine runs at a slightly different operating point (kept across restarts).
        self.base = prof.get("base") or {s: BASE[s][0] + rng.gauss(0, BASE[s][1] * 0.8) for s in SENSORS}
        self.noise = prof.get("noise") or {s: BASE[s][1] * rng.uniform(0.85, 1.15) for s in SENSORS}
        for s in SENSORS:  # machines stored before a signal was added get an operating point for it
            self.base.setdefault(s, BASE[s][0] + rng.gauss(0, BASE[s][1] * 0.8))
            self.noise.setdefault(s, BASE[s][1] * rng.uniform(0.85, 1.15))
        self.profile = prof.get("condition")  # {kind, fault, sensor, start_ts, fail_ts, k} or None (healthy)
        self.scale = np.array([BASE[s][1] * 3 for s in SENSORS])  # for loss normalisation
        self.history: deque = deque(maxlen=HISTORY)
        self.mid: deque = deque(maxlen=MID_KEEP)
        self.long: deque = deque(maxlen=LONG_KEEP)
        self._acc = {"mid": None, "long": None}
        self.lifecycle = (stored or {}).get("lifecycle") or "running"
        self.down_since = _epoch((stored or {}).get("down_since"))
        self.work_order = (stored or {}).get("work_order_id")
        self.version = 0
        self.version_offset = int((stored or {}).get("model_version") or 0)
        self._reset_learning()

    def _reset_learning(self):
        self.n = 0
        self.mean = np.zeros(len(SENSORS))
        self.m2 = np.zeros(len(SENSORS))
        self.trained_tick = None
        self.trained_ms = None
        self.mu = None
        self.inv = None
        self.std = None
        self.ref = None
        self.train_buf: list[np.ndarray] = []
        self.score_s = 0.0
        self.psi = {s: 0.0 for s in SENSORS}
        self.drift_ticks = 0
        self.drift = None  # {"kind": "classifying"|"degradation", "sensor":..., "since": tick, ...}
        self.last_retrain = None  # {"sensor", "tick", "direction"}
        self.thr = None
        self.status = "learning"
        self.alerted = set()
        self.rul = None
        self.rul_fit = None
        self.crit_since = None
        self.first_warning_ms = None
        self._health = None
        self._top = None

    @property
    def model_version(self) -> int:
        return self.version + self.version_offset

    # -- learning ----------------------------------------------------------
    def prequential_loss(self, x: np.ndarray) -> float:
        """NLL of x under the current estimate (predict before update), standardised units."""
        z = x / self.scale
        mu = self.mean / self.scale
        n0, prior_var = 3.0, 1.0
        var_hat = (self.m2 / self.scale ** 2) / max(self.n - 1, 1) if self.n > 1 else np.ones_like(z)
        var = (self.n * var_hat + n0 * prior_var) / (self.n + n0)
        mu_eff = mu if self.n > 0 else z
        return float(np.mean(0.5 * ((z - mu_eff) ** 2 / var + np.log(2 * np.pi * var))))

    def learn(self, x: np.ndarray):
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)
        self.train_buf.append(x)

    def fit(self, samples: np.ndarray, tick: int, alpha: float = 1e-3):
        mu = samples.mean(axis=0)
        cov = np.cov(samples, rowvar=False)
        cov = 0.85 * cov + 0.15 * np.diag(np.diag(cov))  # shrinkage for a stable inverse
        self.mu, self.inv = mu, np.linalg.inv(cov)
        self.std = np.sqrt(np.diag(cov))
        self.ref = samples.copy()
        self.version += 1
        self.trained_tick = tick
        self.thr = round(hotelling_threshold(len(samples), len(SENSORS), alpha), 2)
        self.score_s = math.sqrt(len(SENSORS))  # expected distance under the model
        self.drift_ticks = 0
        self.psi = {s: 0.0 for s in SENSORS}

    # -- scoring -----------------------------------------------------------
    def score(self, x: np.ndarray) -> tuple[float, str, dict]:
        d = x - self.mu
        maha = float(math.sqrt(max(d @ self.inv @ d, 0.0)))
        z = {s: float(d[i] / self.std[i]) for i, s in enumerate(SENSORS)}
        top = max(z, key=lambda s: abs(z[s]))
        return maha, top, z


class MonitoringEngine:
    def __init__(self):
        self.active = False
        self.paused = False
        self.on_event = None   # callable(event) for every event (realtime runner persists alerts)
        self.on_state = None   # callable(machine_record) when a machine's lifecycle or status changes
        self._reset_state()

    def _reset_state(self):
        self.session_id = None
        self.config = {}
        self.tick = 0
        self.sim_start = None
        self.realtime = False
        self.interval = 1.0
        self.tick_epoch = None   # realtime: wall-clock time (epoch s) of the current sample
        self.next_due = None     # realtime: when the next sample is due
        self.machines: dict[str, MachineModel] = {}
        self.loss_curve: list[dict] = []
        self.events: deque = deque(maxlen=400)
        self.alerts: dict[str, dict] = {}
        self.scenario = {}
        self.started_at = None
        self._pending_events: list[dict] = []
        self.tiers = dict(TIERS)
        self.trip_after_s = 10.0

    # -- lifecycle ---------------------------------------------------------
    def start(self, cfg: dict, stored: dict | None = None) -> dict:
        """stored: {machine_id: machine record} from a previous session (realtime only)."""
        self._reset_state()
        seed = cfg.get("seed") or random.randrange(1, 10**6)
        rng = random.Random(seed)
        self.rng = rng
        self.np_rng = np.random.default_rng(seed)
        realtime = cfg.get("clock") == "realtime"
        stored = (stored or {}) if realtime else {}
        sensor_machines = set(D.table("sensor_readings")["machine_id"].unique())
        all_m = [m for m in D.records(D.table("machines")) if m["machine_id"] in sensor_machines]
        chosen = cfg.get("machines") or [m["machine_id"] for m in all_m]
        fleet = [m for m in all_m if m["machine_id"] in chosen] or all_m
        self.machines = {m["machine_id"]: MachineModel(m, rng, stored.get(m["machine_id"])) for m in fleet}
        ids = list(self.machines)
        training = int(cfg.get("training_ticks", 50))
        self.tiers = {k: float((cfg.get("tiers") or {}).get(k, v)) for k, v in TIERS.items()}
        self.trip_after_s = float(cfg.get("trip_after_s", 10))

        fault_key = cfg.get("fault") if cfg.get("fault") in FAULTS else rng.choice(list(FAULTS))
        target = rng.choice(ids)
        self.scenario = {
            "fault": fault_key, "fault_label": FAULTS[fault_key]["label"], "sensor": FAULTS[fault_key]["sensor"],
            "machine_id": target, "onset_tick": training + rng.randint(12, 35),
            "ticks_to_failure": rng.randint(70, 110), "shape": round(rng.uniform(1.35, 1.9), 2),
            "seed": seed,
        }
        self.realtime = realtime
        self.interval = max(0.25, float(cfg.get("sample_interval_s") or 1.0))
        self.config = {
            "machines": ids, "training_ticks": training, "speed": float(cfg.get("speed", 2)),
            "sensitivity": cfg.get("sensitivity", "medium"), "psi_threshold": float(cfg.get("psi_threshold", 0.25)),
            "drift_window": int(cfg.get("drift_window", 40)),
            "minutes_per_tick": self.interval / 60 if self.realtime else int(cfg.get("minutes_per_tick", 10)),
            "clock": "realtime" if self.realtime else "simulated", "sample_interval_s": self.interval if self.realtime else None,
            "fault_mode": "random" if cfg.get("fault") not in FAULTS else fault_key,
            "benign_drift": bool(cfg.get("benign_drift", True)), "tiers": self.tiers, "trip_after_s": self.trip_after_s,
        }
        alpha = SENSITIVITY_ALPHA.get(self.config["sensitivity"], 1e-3)
        self.alpha = alpha
        self.config["score_threshold"] = round(hotelling_threshold(training, len(SENSORS), alpha), 2)
        self.config["psi_critical"] = round(max(self.config["psi_threshold"],
                                                psi_critical(training, self.config["drift_window"])), 3)
        self.session_id = uuid.uuid4().hex[:8].upper()
        self.sim_start = datetime.now().replace(second=0, microsecond=0)
        self.started_at = time.time()
        self.active, self.paused = True, False
        if self.realtime:
            return self._start_realtime(cfg, ids, training, rng)
        if cfg.get("benign_drift", True) and len(ids) > 1:
            other = rng.choice([i for i in ids if i != target])
            self.scenario["benign"] = {"machine_id": other, "sensor": rng.choice(["temp_c", "power_kw"]),
                                       "onset_tick": training + rng.randint(8, 30), "sigma_shift": round(rng.uniform(3.2, 4.2), 2)}
        self._event("info", None, f"Monitoring started for {len(ids)} machines with a {training}-sample learning window.",
                    detail=f"Session {self.session_id}; anomaly limit {self.config['score_threshold']}σ (Hotelling T², α={alpha:g}); "
                           f"PSI critical {self.config['psi_critical']} (calibrated for n={training}/{self.config['drift_window']})")
        self._event("info", None, "Learning each machine's normal operating pattern.", detail="Online baseline training started")
        return self.state()

    # -- real-clock start ----------------------------------------------------
    def _assign_conditions(self, rng: random.Random, now: float, near_failure: bool):
        fresh = [m for m in self.machines.values() if m.profile is None]
        stored_any = any(m.profile is not None for m in self.machines.values())
        running_healthy = lambda: [m for m in self.machines.values()
                                   if m.lifecycle == "running" and not (m.profile or {}).get("fault")]

        def give(m: MachineModel, kind: str):
            c = CONDITIONS[kind]
            remaining = rng.uniform(*c["remaining_h"]) * 3600
            life = max(rng.uniform(*c["life_h"]) * 3600, remaining * 1.25)
            fault = rng.choice(list(FAULTS))
            m.profile = {"kind": kind, "fault": fault, "sensor": FAULTS[fault]["sensor"], "k": c["k"],
                         "start_ts": now + remaining - life, "fail_ts": now + remaining}

        if not stored_any:
            order = list(self.machines.values())
            rng.shuffle(order)
            plan = []
            if near_failure and len(order) >= 2:
                plan.append("near_failure")
            if len(order) >= 3:
                plan.append("plan")
            if len(order) >= 4:
                plan += ["wear"] * (2 if len(order) >= 8 else 1)
            for m, kind in zip(order, plan):
                give(m, kind)
            for m in order[len(plan):]:
                m.profile = {"kind": "healthy", "fault": None}
        else:
            for m in fresh:
                m.profile = {"kind": "healthy", "fault": None}
            soon = [m for m in self.machines.values() if m.lifecycle in ("running", "commissioning")
                    and (m.profile or {}).get("fault") and m.profile["fail_ts"] - now < 3 * 3600]
            if near_failure and not soon:
                cands = running_healthy()
                if cands:
                    give(rng.choice(cands), "near_failure")

    def _start_realtime(self, cfg: dict, ids: list, training: int, rng: random.Random) -> dict:
        now = time.time()
        self._assign_conditions(rng, now, bool(cfg.get("near_failure_machine", True)))
        healthy = [m.id for m in self.machines.values() if not (m.profile or {}).get("fault") and m.lifecycle == "running"]
        if cfg.get("benign_drift", True) and healthy:
            self.scenario["benign"] = {"machine_id": rng.choice(healthy), "sensor": rng.choice(["temp_c", "power_kw"]),
                                       "onset_tick": rng.randint(45, 120), "sigma_shift": round(rng.uniform(3.2, 4.2), 2)}
        else:
            self.scenario.pop("benign", None)
        self.scenario.update({"conditions": {m.id: m.profile for m in self.machines.values()}})
        self.tick_epoch = now - (WARMUP + 1) * self.interval
        self._event("info", None, f"Monitoring started for {len(ids)} machines, one sample every {fmt_hours(self.interval / 3600)}.",
                    detail=f"Session {self.session_id}; {training}-sample baselines; anomaly limit {self.config['score_threshold']}σ "
                           f"(Hotelling T², α={self.alpha:g}); PSI critical {self.config['psi_critical']}")

        # failures that happened while nobody was watching
        for m in self.machines.values():
            p = m.profile or {}
            if m.lifecycle in ("running", "commissioning") and p.get("fault") and p["fail_ts"] <= now:
                m.lifecycle, m.down_since = "tripped", p["fail_ts"]
                m.status = "down"
                self.tick_epoch = p["fail_ts"]
                self._event("critical", m.id, f"{m.id} {m.name} stopped at {datetime.fromtimestamp(p['fail_ts']):%a %H:%M} "
                                              f"while monitoring was off: {SENSOR_LABEL[p['sensor']]} reached its critical limit.",
                            kind="trip", sensor=p["sensor"], offline=True, detail="Protective stop recorded on reconnect")
                self.tick_epoch = now - (WARMUP + 1) * self.interval

        # reference baselines from each machine's normal period, then 7 days of history
        losses = []
        for m in self.machines.values():
            p = m.profile or {}
            ref_end = min(p["start_ts"], now) if p.get("fault") else (m.down_since or now) - WARMUP * self.interval
            curve = []
            for i in range(training):
                x = self._sample_vector(m, ref_end - (training - i) * self.interval, healthy=True)
                curve.append(m.prequential_loss(x))
                m.learn(x)
            losses.append(curve)
            if m.lifecycle in ("running", "commissioning"):
                m.fit(np.array(m.train_buf), 0, self.alpha)
                m.trained_ms = int(ref_end * 1000)
                m.status = "healthy"
                if m.lifecycle == "commissioning":
                    # monitoring stopped mid-commissioning: the new normal is learned from the post-repair
                    # period now, so the machine returns to service (and its work order closes) here
                    m.lifecycle = "running"
                    self._event("success", m.id, f"{m.id} {m.name} is back in service. Its new normal has been "
                                f"learned and early-warning scoring is on again (model v{m.model_version}).",
                                kind="recommissioned", detail=f"Baseline fitted on {training} post-repair samples "
                                "when monitoring restarted")
            self._backfill(m, now)
        if losses:
            avg = np.mean(np.array(losses), axis=0)
            self.loss_curve = [{"tick": i + 1, "loss": round(float(v), 4)} for i, v in enumerate(avg)]
            self._event("success", None, f"Learned the normal operating pattern of {len(self.machines)} machines from their most "
                                         f"recent normal period. Early-warning scoring is on.",
                        detail=f"Baseline v1 per machine on {training} samples; prequential NLL "
                               f"{self.loss_curve[0]['loss']:.2f} → {self.loss_curve[-1]['loss']:.2f}")
        for m in self.machines.values():
            if m.lifecycle in DOWN:
                m.status = "down"
        # replay the last few samples so the live charts open populated
        first = now - WARMUP * self.interval
        for i in range(WARMUP):
            self.step(ts=first + i * self.interval)
        self.next_due = self.tick_epoch + self.interval
        for m in self.machines.values():
            self._emit_state(m)
        return self.state()

    def _backfill(self, m: MachineModel, now: float):
        """Aggregates for every bucket before the one the first live sample lands in (or the stop time)."""
        end = m.down_since or (now - WARMUP * self.interval)
        for width, keep, series in ((LONG_S, LONG_KEEP, m.long), (MID_S, MID_KEEP, m.mid)):
            scale = 1 / math.sqrt(max(width / self.interval, 1))
            stop = math.floor(end / width) * width
            t = max(stop - keep * width, (math.floor((now - keep * width) / width) + 1) * width)
            while t < stop:
                series.append(self._agg_point(m, t + width / 2, scale, t))
                t += width

    def _agg_point(self, m: MachineModel, t: float, scale: float, bucket: float) -> dict:
        x = self._sample_vector(m, t, noise_scale=scale)
        return {"ms": int(bucket * 1000), **{s: round(float(x[i]), 4) for i, s in enumerate(SENSORS)}}

    def stop(self):
        if self.active:
            self._event("info", None, f"Monitoring stopped after {self.tick} samples.", detail=f"Session {self.session_id}")
            self._broadcast({"type": "stopped", "events": list(self.events)[-1:]})
        self.active = False
        self.paused = False

    def set_paused(self, paused: bool):
        if self.realtime and self.paused and not paused:
            self.next_due = time.time()  # resume on the real clock; the pause shows as a gap
        self.paused = paused
        self._broadcast({"type": "paused", "paused": paused})

    def set_speed(self, speed: float):
        if self.realtime:
            return  # the real clock sets the pace
        self.config["speed"] = max(0.5, min(8.0, float(speed)))
        self._broadcast({"type": "speed", "speed": self.config["speed"]})

    def safe_step(self, ts: float | None = None) -> dict | None:
        """step() that records an engine error as an event instead of raising."""
        try:
            return self.step(ts)
        except Exception as e:  # keep the stream alive; surface the problem
            self._event("critical", None, f"Engine error: {e}")
            return None

    def tick_interval(self) -> float:
        if self.realtime:
            return self.interval
        return 1.0 / max(self.config.get("speed", 1.0), 0.5) if self.active else 0.25

    def advance_to(self, now: float) -> list[dict]:
        """Realtime: produce every sample due by `now`, each stamped with its due time."""
        if not (self.active and self.realtime) or self.paused or now < self.next_due:
            return []
        behind = int((now - self.next_due) // self.interval) + 1
        if behind > MAX_CATCHUP:
            gap = now - self.next_due
            self.next_due = now
            behind = 1
            self._event("info", None, f"Telemetry resumed after a {fmt_hours(gap / 3600)} gap.",
                        detail="Scoring continues on the live stream")
        out = []
        for _ in range(behind):
            p = self.safe_step(self.next_due)
            self.next_due += self.interval
            if p:
                out.append(p)
        return out

    def seconds_to_next(self, now: float) -> float:
        if self.realtime and self.next_due is not None:
            return max(0.0, self.next_due - now)
        return self.tick_interval()

    def plant_now(self) -> float:
        """Epoch of the current sample on the plant clock (wall clock in realtime, virtual otherwise)."""
        if self.realtime and self.tick_epoch is not None:
            return self.tick_epoch
        return (self.sim_start + timedelta(minutes=self.tick * self.config.get("minutes_per_tick", 10))).timestamp()

    # -- physics -----------------------------------------------------------
    def _sample_vector(self, m: MachineModel, t: float, noise_scale: float = 1.0, healthy: bool = False) -> np.ndarray:
        """Real clock: the machine's signals at time t from its condition (wear as a function of time)."""
        frac = 0.0 if healthy else wear_fraction(m.profile, t)
        p = m.profile or {}
        out = np.empty(len(SENSORS))
        for i, s in enumerate(SENSORS):
            v, noise = m.base[s], m.noise[s]
            if frac > 0:
                th = SENSOR_THRESHOLDS[s]
                sign = 1 if th["direction"] == "upper" else -1
                if s == p["sensor"]:
                    v += sign * abs(th["critical"] - m.base[s]) * frac
                    noise *= 1 + 0.6 * min(frac, 1.2)
                elif s in FAULTS[p["fault"]]["couple"]:
                    v += sign * FAULTS[p["fault"]]["couple"][s] * abs(th["warning"] - m.base[s]) * min(frac, 1.2)
            b = self.scenario.get("benign")
            if not healthy and b and m.id == b["machine_id"] and s == b["sensor"] and self.tick >= b["onset_tick"]:
                v += min((self.tick - b["onset_tick"]) / 4, 1.0) * b["sigma_shift"] * BASE[s][1]
            out[i] = max(float(v + self.np_rng.normal(0, noise * noise_scale)), 0.05 * BASE[s][0])
        return out

    def _reading(self, m: MachineModel) -> dict:
        if self.realtime:
            x = self._sample_vector(m, self.tick_epoch)
            return {s: round(float(x[i]), 3) for i, s in enumerate(SENSORS)}
        sc = self.scenario
        vals = {}
        progress = 0.0
        if m.id == sc["machine_id"] and self.tick >= sc["onset_tick"]:
            progress = min((self.tick - sc["onset_tick"]) / sc["ticks_to_failure"], 1.15)
        for s in SENSORS:
            noise = m.noise[s]
            v = m.base[s]
            if progress > 0:
                th = SENSOR_THRESHOLDS[s]
                sign = 1 if th["direction"] == "upper" else -1
                if s == sc["sensor"]:
                    dist = abs(th["critical"] - m.base[s]) * 1.06
                    v += sign * dist * progress ** sc["shape"]
                    noise *= 1 + 0.9 * progress
                elif s in FAULTS[sc["fault"]]["couple"]:
                    v += sign * FAULTS[sc["fault"]]["couple"][s] * abs(th["warning"] - m.base[s]) * progress
            b = sc.get("benign")
            if b and m.id == b["machine_id"] and s == b["sensor"] and self.tick >= b["onset_tick"]:
                ramp = min((self.tick - b["onset_tick"]) / 4, 1.0)
                v += ramp * b["sigma_shift"] * BASE[s][1]
            # physical floor: flows, pressures and vibration cannot go negative
            vals[s] = round(max(float(v + self.np_rng.normal(0, noise)), 0.05 * BASE[s][0]), 3)
        return vals

    def _aggregate(self, m: MachineModel, t: float, vals: dict):
        for name, width, series in (("mid", MID_S, m.mid), ("long", LONG_S, m.long)):
            bucket = math.floor(t / width) * width
            acc = m._acc[name]
            if acc and acc["b"] != bucket:
                series.append({"ms": int(acc["b"] * 1000), **{s: round(acc[s] / acc["n"], 4) for s in SENSORS}})
                acc = None
                if name == "mid":
                    m.rul_fit = None  # refit on the new minute
            if acc is None:
                acc = m._acc[name] = {"b": bucket, "n": 0, **{s: 0.0 for s in SENSORS}}
            acc["n"] += 1
            for s in SENSORS:
                acc[s] += vals[s]

    # -- events ------------------------------------------------------------
    def _event(self, level: str, machine_id, message: str, **extra):
        ev = {"id": uuid.uuid4().hex[:10], "tick": self.tick, "sim_time": self._sim_time(), "ms": self._ms_for(self.tick), "level": level,
              "machine_id": machine_id, "message": message, "session_id": self.session_id, **extra}
        self.events.append(ev)
        self._pending_events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception as e:  # never break sampling, but leave a trace for operators
                err = {"id": uuid.uuid4().hex[:10], "tick": self.tick, "sim_time": ev["sim_time"], "ms": ev["ms"],
                       "level": "warning", "machine_id": machine_id, "kind": "automation_error",
                       "message": f"Follow-up for '{extra.get('kind') or level}' failed: {str(e).splitlines()[0][:200]}",
                       "session_id": self.session_id}
                self.events.append(err)
                self._pending_events.append(err)
        return ev

    def machine_record(self, m: MachineModel) -> dict:
        r = m.rul or {}
        return {"machine_id": m.id, "machine_name": m.name, "line": m.line, "lifecycle": m.lifecycle,
                "status": "down" if m.lifecycle in DOWN else m.status,
                "condition": (m.profile or {}).get("kind"), "fault_mode": (m.profile or {}).get("fault"),
                "fault_sensor": (m.profile or {}).get("sensor"),
                "health_pct": None if m.lifecycle in DOWN else m._health, "rul_hours": r.get("hours"),
                "model_version": m.model_version, "work_order_id": m.work_order, "down_since": _iso(m.down_since),
                "updated_at": _iso(time.time()),
                "profile": {"base": m.base, "noise": m.noise, "condition": m.profile}}

    def _emit_state(self, m: MachineModel):
        if self.on_state and self.realtime:
            try:
                self.on_state(self.machine_record(m))
            except Exception:
                pass

    def _sim_time(self) -> str:
        """Timestamp of the current sample (wall clock in realtime, virtual clock otherwise)."""
        if self.realtime and self.tick_epoch is not None:
            return datetime.fromtimestamp(self.tick_epoch).astimezone().isoformat(timespec="seconds")
        if not self.sim_start:
            return ""
        return (self.sim_start + timedelta(minutes=self.tick * self.config.get("minutes_per_tick", 10))).isoformat(timespec="minutes")

    def _ms_for(self, tick: int) -> int:
        """Epoch milliseconds of a (past or forecast) sample index, for time axes."""
        if self.realtime and self.tick_epoch is not None:
            return int((self.tick_epoch + (tick - self.tick) * self.interval) * 1000)
        if not self.sim_start:
            return 0
        return int((self.sim_start + timedelta(minutes=tick * self.config.get("minutes_per_tick", 10))).timestamp() * 1000)

    # -- sampling ----------------------------------------------------------
    def step(self, ts: float | None = None) -> dict:
        self._pending_events = []
        self.tick += 1
        if self.realtime:
            self.tick_epoch = ts if ts is not None else time.time()
        cfg = self.config
        training = cfg["training_ticks"]
        losses = []
        for m in self.machines.values():
            if m.lifecycle in DOWN:
                continue
            vals = self._reading(m)
            x = np.array([vals[s] for s in SENSORS])
            rec = {"tick": self.tick, "t": self._sim_time(), "ms": self._ms_for(self.tick), **vals}
            if m.version == 0 or m.lifecycle == "commissioning":
                if not self.realtime:
                    losses.append(m.prequential_loss(x))
                m.learn(x)
                rec.update(score=None, health=None)
                if m.n >= training:
                    m.fit(np.array(m.train_buf), self.tick, self.alpha)
                    m.trained_ms = rec["ms"]
                    m.status = "healthy"
                    if m.lifecycle == "commissioning":
                        m.lifecycle = "running"
                        self._event("success", m.id, f"{m.id} {m.name} is back in service. Its new normal has been learned "
                                                     f"and early-warning scoring is on again (model v{m.model_version}).",
                                    kind="recommissioned", detail=f"Baseline refit on {m.n} post-repair samples")
                        self._emit_state(m)
            else:
                self._monitor(m, x, vals, rec)
            m.history.append(rec)
            if self.realtime:
                self._aggregate(m, self.tick_epoch, vals)

        if losses:
            self.loss_curve.append({"tick": self.tick, "loss": round(float(np.mean(losses)), 4)})
            if self.tick == training:
                first, last = self.loss_curve[0]["loss"], self.loss_curve[-1]["loss"]
                self._event("success", None, f"Learned the normal operating pattern of {len(self.machines)} machines. "
                                             f"Early-warning scoring is on.",
                            detail=f"Baseline model v1 trained on {self.tick} samples × {len(self.machines)} machines; "
                                   f"prequential NLL {first:.2f} → {last:.2f}")
        return self._tick_payload()

    def _trend_test(self, m: MachineModel, sensor: str, until_ms: int) -> dict | None:
        """Real clock: is there a significant trend toward the limit in the period before the change?"""
        th = SENSOR_THRESHOLDS[sensor]
        sig = m.std[SENSORS.index(sensor)]
        best = None
        for series, span_h in ((m.mid, 6), (m.long, 72), (m.long, 168)):
            pts = [p for p in series if until_ms - span_h * 3.6e6 <= p["ms"] < until_ms]
            if len(pts) < 12:
                continue
            t = np.array([(p["ms"] - pts[0]["ms"]) / 3.6e6 for p in pts])
            y = np.array([p[sensor] for p in pts])
            lr = stats.linregress(t, y)
            toward = lr.slope > 0 if th["direction"] == "upper" else lr.slope < 0
            moved = abs(lr.slope) * (t[-1] - t[0]) / sig
            res = {"slope_per_hour": float(lr.slope), "p_value": float(lr.pvalue), "r2": float(lr.rvalue ** 2),
                   "moved": float(moved), "window_h": span_h, "samples": len(pts), "toward": bool(toward)}
            if toward and lr.pvalue < 1e-2 and moved > 1.0:
                return res
            best = best or res
        return best and {**best, "degrading": False}

    def _monitor(self, m: MachineModel, x: np.ndarray, vals: dict, rec: dict):
        cfg = self.config
        thr = m.thr
        maha, top, z = m.score(x)
        m.score_s = 0.35 * maha + 0.65 * m.score_s
        W = cfg["drift_window"]
        since_fit = self.tick - m.trained_tick
        n_recent = min(W, since_fit) - 1
        recent_hist = [h for h in list(m.history)[-n_recent:] if h["tick"] > m.trained_tick] if n_recent > 0 else []

        # drift (PSI) over the recent window against the reference the model was fitted on
        psi_crit = 0.0
        if since_fit >= 20 or (self.realtime and len(recent_hist) >= 19):
            recent = np.array([[h[s] for s in SENSORS] for h in recent_hist] + [x])
            m.psi = {s: round(_psi(m.ref[:, i], recent[:, i]), 3) for i, s in enumerate(SENSORS)}
            psi_crit = max(cfg["psi_threshold"], psi_critical(len(m.ref), len(recent)))
        m.psi_crit = round(psi_crit, 3) if psi_crit else None
        worst_sensor = max(m.psi, key=m.psi.get)
        worst_psi = m.psi[worst_sensor]
        breach = bool(psi_crit and worst_psi >= psi_crit)
        if breach and self.realtime:
            # a long-running stream runs millions of tests: also require a shift that matters (>= 1.5 baseline sd)
            i = SENSORS.index(worst_sensor)
            shift = abs(float(np.mean([h[worst_sensor] for h in recent_hist] + [x[i]])) - m.mu[i]) / m.std[i]
            breach = shift >= 1.5
        m.drift_ticks = m.drift_ticks + 1 if breach else 0
        label = SENSOR_LABEL[worst_sensor]

        # 1) persistent PSI breach opens a drift candidate
        if m.drift is None and m.drift_ticks >= 3:
            m.drift = {"kind": "classifying", "sensor": worst_sensor, "since": self.tick, "since_ms": rec["ms"], "psi": worst_psi}
            direction = float(np.sign(x[SENSORS.index(worst_sensor)] - m.mu[SENSORS.index(worst_sensor)]))
            lr_ = m.last_retrain
            if lr_ and lr_["sensor"] == worst_sensor and lr_["direction"] == direction and self.tick - lr_["tick"] <= RECUR_WINDOW:
                m.drift["kind"] = "degradation"
                m.drift["recurring"] = True
                self._event("warning", m.id, f"{m.id} {label.lower()} moved again in the same direction soon after the model "
                                             f"re-learned it. This is a slow wear pattern, not a process change. Forecasting time to failure.",
                            kind="degradation", sensor=worst_sensor,
                            detail=f"Recurring drift {self.tick - lr_['tick']} samples after retrain v{m.model_version}; retraining suppressed")
            else:
                self._event("warning", m.id, f"{m.id} {label.lower()} has moved away from its normal pattern. "
                                             f"Checking whether this is wear or a process change.",
                            kind="drift", sensor=worst_sensor, detail=f"PSI {worst_psi:.2f} ≥ critical {psi_crit:.2f}")
            m.first_warning_ms = m.first_warning_ms or rec["ms"]
        if m.drift:
            m.drift["psi"] = m.psi[m.drift["sensor"]]

        # 2) classify: slope significance (on the long series on the real clock)
        if m.drift and m.drift["kind"] == "classifying" and self.tick - m.drift["since"] >= CLASSIFY_AFTER:
            s_key = m.drift["sensor"]
            seg = [h[s_key] for h in list(m.history)[-(self.tick - m.drift["since"] - TRANSITION):]] + [vals[s_key]]
            label = SENSOR_LABEL[s_key]
            if self.realtime:
                tt = self._trend_test(m, s_key, m.drift["since_ms"])
                degrading = bool(tt and tt.get("degrading", True) and tt["toward"] and tt["p_value"] < 1e-2 and tt["moved"] > 1.0)
                if tt:
                    m.drift.update(slope_per_hour=round(tt["slope_per_hour"], 5), r2=round(tt["r2"], 3),
                                   p_value=float(f"{tt['p_value']:.2e}"), samples=tt["samples"], window_h=tt["window_h"])
                detail = (f"Trend {tt['slope_per_hour']:+.4f} {SENSOR_UNIT[s_key]}/h over {tt['window_h']} h "
                          f"(p={tt['p_value']:.1e}, R² {tt['r2']:.2f})") if tt else "Not enough history for a trend test"
            else:
                lr = stats.linregress(np.arange(len(seg)), seg)
                sig = m.std[SENSORS.index(s_key)]
                moved = abs(lr.slope) * len(seg) / sig
                m.drift.update(slope_per_tick=round(float(lr.slope), 4), r2=round(float(lr.rvalue ** 2), 3),
                               p_value=float(f"{lr.pvalue:.2e}"), samples=len(seg))
                degrading = lr.pvalue < 1e-2 and moved > 1.0
                detail = f"Trend {lr.slope:+.3f}/sample (p={lr.pvalue:.1e}, R² {lr.rvalue ** 2:.2f})"
            if degrading:
                m.drift["kind"] = "degradation"
                self._event("critical" if m.status == "critical" else "warning", m.id,
                            f"{m.id} shows a steady wear trend on {label.lower()}. The model will not re-learn this as normal; "
                            f"forecasting time to failure.", kind="degradation", sensor=s_key, detail=detail)
            else:
                window = np.array([[h[s] for s in SENSORS] for h in list(m.history)[-len(seg) + 1:]] + [x])
                old = m.model_version
                direction = float(np.sign(np.mean(seg) - m.mu[SENSORS.index(s_key)]))
                m.fit(window, self.tick, self.alpha)
                m.trained_ms = rec["ms"]
                m.last_retrain = {"sensor": s_key, "tick": self.tick, "direction": direction}
                self._event("success", m.id, f"{m.id} {label.lower()} settled at a new steady level. This is a process change, "
                                             f"not wear, so the model re-learned its normal (v{old} → v{m.model_version}).",
                            kind="retrain", sensor=s_key,
                            detail=f"No trend ({detail}); refit on {len(window)} post-change samples; new baseline "
                                   f"{m.mu[SENSORS.index(s_key)]:.2f} {SENSOR_UNIT[s_key]}")
                m.drift = None
                m.alerted.discard("anomaly")
                maha, top, z = m.score(x)
                m.score_s = maha

        # degradation follow-up: RUL forecast on whichever sensor fails first
        m.rul = None
        if m.drift and m.drift["kind"] == "degradation":
            if self.realtime:
                m.rul = self._rul_realtime(m, rec["ms"])
            else:
                cands = [r for r in (self._rul(m, s) for s in SENSORS) if r and r["hours"] is not None and r["r2"] > 0.3]
                m.rul = min(cands, key=lambda r: r["hours"]) if cands else self._rul(m, m.drift["sensor"])

        # threshold checks
        breaches = {}
        for s in SENSORS:
            th = SENSOR_THRESHOLDS[s]
            v = vals[s]
            up = th["direction"] == "upper"
            if (v >= th["critical"]) if up else (v <= th["critical"]):
                breaches[s] = "critical"
            elif (v >= th["warning"]) if up else (v <= th["warning"]):
                breaches[s] = "warning"

        # health
        prox_h = 100.0
        for s in SENSORS:
            th = SENSOR_THRESHOLDS[s]
            span = (th["critical"] - m.mu[SENSORS.index(s)])
            frac = (vals[s] - m.mu[SENSORS.index(s)]) / span if span else 0
            prox_h = min(prox_h, 100 * (1 - max(0.0, min(frac, 1.0))))
        if self.realtime:
            # distance to the nearest critical limit, with a small penalty for unusual multi-signal behaviour
            health = round(max(0.0, prox_h * (1 - min(0.25, max(0.0, m.score_s - thr) * 0.03))), 1)
            if m._health is not None and health > 0:
                health = round(0.25 * health + 0.75 * m._health, 1)  # smooth sample noise; a limit breach shows at once
        else:
            stat_h = 100 - max(0.0, m.score_s - 2.2) * 16
            health = round(max(0.0, min(stat_h, prox_h, 100.0)), 1)

        prev = m.status
        rul_h = m.rul["hours"] if m.rul else None
        tiers = self.tiers
        if "critical" in breaches.values() or (rul_h is not None and rul_h < tiers["critical"]):
            m.status = "critical"
        elif "warning" in breaches.values() or m.score_s > thr or (m.drift and m.drift["kind"] == "degradation"):
            m.status = "warning"
        elif m.drift and m.drift["kind"] == "classifying":
            m.status = "drift"
        else:
            m.status = "healthy"
        if m.status in ("warning", "critical"):
            m.first_warning_ms = m.first_warning_ms or rec["ms"]

        if m.score_s > thr and "anomaly" not in m.alerted:
            m.alerted.add("anomaly")
            self._event("warning", m.id, f"{m.id} is behaving unusually: {SENSOR_LABEL[top].lower()} is "
                                         f"{'well above' if z[top] > 0 else 'well below'} its normal range.",
                        kind="anomaly", sensor=top,
                        detail=f"Anomaly score {m.score_s:.1f}σ > limit {thr}σ; largest contributor {SENSOR_LABEL[top]} (z={z[top]:+.1f})")
        for s, lvl in breaches.items():
            key = f"{lvl}:{s}"
            if key not in m.alerted:
                m.alerted.add(key)
                limit = SENSOR_THRESHOLDS[s][lvl]
                self._event("critical" if lvl == "critical" else "warning", m.id,
                            f"{m.id} {SENSOR_LABEL[s].lower()} {vals[s]} {SENSOR_UNIT[s]} passed the {lvl} limit ({limit} {SENSOR_UNIT[s]}).",
                            kind=f"threshold_{lvl}", sensor=s)
        if m.rul and rul_h is not None:
            rng_txt = f"likely {fmt_hours(m.rul['low'])}–{fmt_hours(m.rul['high'])}" if m.rul.get("low") is not None else ""
            sens = SENSOR_LABEL[m.rul["sensor"]].lower()
            for tier, msg in (("plan", "Plan maintenance."), ("urgent", "Schedule maintenance urgently.")):
                if rul_h < tiers[tier] and f"rul_{tier}" not in m.alerted:
                    m.alerted.add(f"rul_{tier}")
                    if tier == "plan" and rul_h < tiers["urgent"]:
                        continue  # the urgent notice covers it
                    self._event("warning", m.id, f"{m.id} is expected to reach its {sens} limit in about {fmt_hours(rul_h)}"
                                                 f"{' (' + rng_txt + ')' if rng_txt else ''}. {msg}",
                                kind=f"rul_{tier}", sensor=m.rul["sensor"], rul_hours=rul_h,
                                detail=f"Linear fit R² {m.rul['r2']:.2f}, slope {m.rul['slope_per_hour']:+.4g} {SENSOR_UNIT[m.rul['sensor']]}/h")

        if m.status == "critical" and prev != "critical":
            sensor = next((s for s, l in breaches.items() if l == "critical"), (m.drift or {}).get("sensor", top))
            self.alerts[m.id] = {"machine_id": m.id, "name": m.name, "line": m.line, "sensor": sensor,
                                 "value": vals[sensor], "tick": self.tick, "sim_time": self._sim_time(),
                                 "rul_hours": rul_h, "health": health}
            if "escalate" not in m.alerted:
                m.alerted.add("escalate")
                self._event("critical", m.id, f"{m.id} is critical. An 8D investigation is recommended.", kind="escalate", sensor=sensor)

        rec.update(score=round(m.score_s, 3), health=health)
        m._health = health
        m._top = top
        if prev != m.status:
            self._emit_state(m)

        # protective stop: a critical limit held for trip_after_s
        if self.realtime and "critical" in breaches.values():
            now = self.tick_epoch
            m.crit_since = m.crit_since or now
            if now - m.crit_since >= self.trip_after_s - 1e-6:
                sensor = next(s for s, l in breaches.items() if l == "critical")
                self._trip(m, sensor, vals[sensor])
        else:
            m.crit_since = None

    def _trip(self, m: MachineModel, sensor: str, value: float):
        m.lifecycle, m.down_since, m.status = "tripped", self.tick_epoch, "down"
        m.crit_since = None
        self.alerts.setdefault(m.id, {"machine_id": m.id, "name": m.name, "line": m.line, "sensor": sensor, "value": value,
                                      "tick": self.tick, "sim_time": self._sim_time(), "rul_hours": 0, "health": 0})
        self.alerts[m.id].update(tripped=True, down_since=self._sim_time())
        self._event("critical", m.id, f"{m.id} {m.name} stopped automatically: {SENSOR_LABEL[sensor].lower()} stayed above "
                                      f"its critical limit for {self.trip_after_s:g} s. The machine is down and needs maintenance.",
                    kind="trip", sensor=sensor, value=value,
                    detail=f"Protective stop at {value} {SENSOR_UNIT[sensor]} (critical {SENSOR_THRESHOLDS[sensor]['critical']})")
        self._emit_state(m)

    # -- operator actions (realtime) ------------------------------------------
    def set_work_order(self, machine_id: str, wo_id: str | None):
        m = self.machines[machine_id]
        m.work_order = wo_id
        self._emit_state(m)

    def take_offline(self, machine_id: str, reason: str = "planned maintenance"):
        m = self.machines[machine_id]
        if m.lifecycle in DOWN:
            return
        m.lifecycle, m.down_since, m.status = "stopped", time.time(), "down"
        self._event("info", m.id, f"{m.id} {m.name} was taken offline for {reason}.", kind="stopped")
        self._emit_state(m)

    def start_repair(self, machine_id: str):
        m = self.machines[machine_id]
        if m.lifecycle == "running" or m.lifecycle == "commissioning":
            self.take_offline(machine_id)
        m.lifecycle = "in_repair"
        self._event("info", m.id, f"Repair started on {m.id} {m.name}.", kind="repair_started")
        self._emit_state(m)

    def complete_repair(self, machine_id: str):
        """Fault cleared: new healthy condition, re-learn the baseline from live samples (commissioning)."""
        m = self.machines[machine_id]
        p = m.profile or {}
        if p.get("fault") == "bearing_wear" or p.get("sensor") == "vibration_mm_s":
            m.base["vibration_mm_s"] = max(m.base["vibration_mm_s"] - 0.1, 1.2)  # new bearings run a little quieter
        m.profile = {"kind": "healthy", "fault": None, "repaired_at": time.time()}
        offset = m.model_version
        m.version, m.version_offset = 0, offset
        m._reset_learning()
        m.lifecycle, m.down_since = "commissioning", None
        self.alerts.pop(m.id, None)
        self._event("success", m.id, f"Repair completed on {m.id} {m.name}. Commissioning: learning its new normal before "
                                     f"alerts are switched back on.", kind="repair_completed")
        self._emit_state(m)

    # -- RUL -----------------------------------------------------------------
    def _rul_realtime(self, m: MachineModel, now_ms: int) -> dict | None:
        """Fit the degrading signals on the 1-minute and 10-minute series; refit each minute, count down in between."""
        if m.rul_fit is None:
            cands = []
            for s in SENSORS:
                for series, span_h, label in ((m.mid, 0.5, "30 min"), (m.mid, 6, "6 h"), (m.long, 48, "48 h")):
                    r = self._fit_series(m, s, series, span_h, now_ms, label)
                    if r and r["hours"] is not None and r["r2"] > 0.3:
                        cands.append(r)
            if not cands:
                s = m.drift["sensor"]
                cands = [r for r in (self._fit_series(m, s, m.mid, 6, now_ms, "6 h"),) if r]
            m.rul_fit = min(cands, key=lambda r: r["hours"] if r["hours"] is not None else 1e9) if cands else None
            if m.rul_fit:
                m.rul_fit["fitted_ms"] = now_ms
        f = m.rul_fit
        if not f:
            return None
        el = (now_ms - f["fitted_ms"]) / 3.6e6
        adj = lambda h: None if h is None else round(max(h - el, 0.0), 4)
        return {**f, "hours": adj(f["hours"]), "low": adj(f["low"]), "high": adj(f["high"])}

    def _fit_series(self, m: MachineModel, sensor: str, series, span_h: float, now_ms: int, label: str) -> dict | None:
        pts = [p for p in series if p["ms"] >= now_ms - span_h * 3.6e6]
        if len(pts) < 8:
            return None
        t = np.array([(p["ms"] - now_ms) / 3.6e6 for p in pts])
        y = np.array([p[sensor] for p in pts])
        (slope, intercept), cov = np.polyfit(t, y, 1, cov=True)
        resid = y - (slope * t + intercept)
        r2 = 1 - np.sum(resid ** 2) / max(np.sum((y - y.mean()) ** 2), 1e-12)
        se = math.sqrt(max(cov[0, 0], 1e-12))
        th = SENSOR_THRESHOLDS[sensor]
        up = th["direction"] == "upper"
        crit = th["critical"]
        now_fit = intercept

        def hours_for(sl):
            if (sl > 0) != up or abs(sl) < 1e-12:
                return None
            return max((crit - now_fit) / sl, 0.0)

        toward = (slope > 0) if up else (slope < 0)
        h = hours_for(slope)
        sgn = 1 if up else -1
        fast, slow = hours_for(slope + 1.96 * se * sgn), hours_for(slope - 1.96 * se * sgn)
        if slow is None and h is not None:
            slow = h * 3
        horizon_h = min(max(h or span_h, span_h * 0.3) * 1.15, 24 * 30)
        rs = float(np.std(resid))
        forecast = []
        for i in range(1, 41):
            dh = horizon_h * i / 40
            v = now_fit + slope * dh
            band = 1.96 * math.sqrt(rs ** 2 + (dh * se) ** 2)
            forecast.append({"ms": int(now_ms + dh * 3.6e6), "v": round(float(v), 4),
                             "lo": round(float(v - band), 4), "hi": round(float(v + band), 4)})
        return {"sensor": sensor, "hours": round(h, 4) if (toward and h is not None) else None, "ticks": None,
                "low": round(fast, 4) if (toward and fast is not None) else None,
                "high": round(slow, 4) if (toward and slow is not None) else None,
                "slope_per_hour": round(float(slope), 5), "r2": round(float(r2), 3), "window": label,
                "critical": crit, "warning": th["warning"], "forecast": forecast}

    def _rul(self, m: MachineModel, sensor: str) -> dict:
        n = 24
        series = np.array([h[sensor] for h in list(m.history)[-n:]])
        if len(series) < 8:
            return None
        t = np.arange(len(series), dtype=float)
        (slope, intercept), cov = np.polyfit(t, series, 1, cov=True)
        resid = series - (slope * t + intercept)
        r2 = 1 - np.sum(resid ** 2) / max(np.sum((series - series.mean()) ** 2), 1e-9)
        se = math.sqrt(max(cov[0, 0], 1e-12))
        th = SENSOR_THRESHOLDS[sensor]
        crit = th["critical"]
        now_fit = slope * t[-1] + intercept
        mpt = self.config["minutes_per_tick"]
        toward = (slope > 0) if th["direction"] == "upper" else (slope < 0)

        def ticks_for(sl):
            if (sl > 0) != (th["direction"] == "upper") or abs(sl) < 1e-9:
                return None
            return max((crit - now_fit) / sl, 0.0)

        ticks = ticks_for(slope)
        fast, slow = ticks_for(slope + 1.96 * se * (1 if th["direction"] == "upper" else -1)), ticks_for(slope - 1.96 * se * (1 if th["direction"] == "upper" else -1))
        horizon = int(min(max(ticks or 0, 10) * 1.2, 80))
        rs = float(np.std(resid))
        forecast = []
        for h in range(1, horizon + 1):
            v = now_fit + slope * h
            band = 1.96 * math.sqrt(rs ** 2 + (h * se) ** 2)
            forecast.append({"tick": self.tick + h, "ms": self._ms_for(self.tick + h), "v": round(float(v), 3),
                             "lo": round(float(v - band), 3), "hi": round(float(v + band), 3)})
        to_h = lambda tk: round(tk * mpt / 60, 1 if mpt >= 1 else 5) if tk is not None else None
        return {"sensor": sensor, "hours": to_h(ticks) if toward else None, "ticks": round(ticks, 2) if (toward and ticks is not None) else None,
                "low": to_h(fast) if toward else None,
                "high": to_h(slow) if toward else None, "slope_per_hour": round(float(slope) * 60 / mpt, 4),
                "r2": round(float(r2), 3), "critical": crit, "warning": th["warning"], "forecast": forecast}

    # -- payloads ----------------------------------------------------------
    def action_tier(self, m: MachineModel) -> str | None:
        if m.lifecycle in DOWN:
            return None
        h = (m.rul or {}).get("hours")
        if h is None:
            return None
        for tier in ("critical", "urgent", "plan"):
            if h < self.tiers[tier]:
                return tier
        return None

    def _machine_view(self, m: MachineModel) -> dict:
        last = m.history[-1] if m.history else {}
        down = m.lifecycle in DOWN
        learning = (m.version == 0 or m.lifecycle == "commissioning") and not down
        return {
            "machine_id": m.id, "name": m.name, "line": m.line, "type": m.type,
            "status": "down" if down else m.status, "lifecycle": m.lifecycle,
            "down_since_ms": int(m.down_since * 1000) if m.down_since else None, "work_order_id": m.work_order,
            "learning_progress": round(min(m.n / max(self.config.get("training_ticks", 1), 1), 1.0) * 100, 1) if learning else None,
            "health": None if (down or learning) else m._health,
            "score": round(m.score_s, 3) if (m.version and not learning) else None, "top_sensor": m._top,
            "values": {s: last.get(s) for s in SENSORS},
            "model": {"version": m.model_version if m.version else 0, "trained_tick": m.trained_tick, "trained_ms": m.trained_ms,
                      "samples": m.n if m.version == 0 else len(m.ref),
                      "threshold": m.thr,
                      "mean": {s: round(float(m.mu[i]), 3) for i, s in enumerate(SENSORS)} if m.version else None,
                      "std": {s: round(float(m.std[i]), 4) for i, s in enumerate(SENSORS)} if m.version else None},
            "psi": m.psi, "psi_critical": getattr(m, "psi_crit", None), "drift": m.drift, "rul": m.rul,
            "action": self.action_tier(m), "first_warning_ms": m.first_warning_ms,
        }

    def _tick_payload(self) -> dict:
        cfg = self.config
        return {
            "type": "tick", "tick": self.tick, "sim_time": self._sim_time(),
            "phase": self.phase(),
            "training_progress": self.training_progress(),
            "loss": self.loss_curve[-1] if self.tick <= cfg["training_ticks"] and self.loss_curve else None,
            "machines": [self._machine_view(m) for m in self.machines.values()],
            "readings": {m.id: m.history[-1] for m in self.machines.values() if m.history},
            "events": self._pending_events, "alerts": list(self.alerts.values()),
        }

    def phase(self) -> str:
        if self.realtime:
            return "monitoring"
        return "learning" if self.tick < self.config["training_ticks"] else "monitoring"

    def training_progress(self) -> float:
        if self.realtime:
            return 100.0
        return round(min(self.tick / self.config["training_ticks"], 1.0) * 100, 1)

    def state(self, reveal: bool = False) -> dict:
        if not self.session_id:
            return {"active": False, "session_id": None, "defaults": self.defaults()}
        cfg = self.config
        out = {
            "active": self.active, "paused": self.paused, "session_id": self.session_id, "config": cfg,
            "tick": self.tick, "sim_time": self._sim_time(), "started_at": self.started_at,
            "phase": self.phase(), "training_progress": self.training_progress(),
            "loss_curve": self.loss_curve,
            "machines": [self._machine_view(m) for m in self.machines.values()],
            "history": {m.id: list(m.history) for m in self.machines.values()},
            "events": list(self.events), "alerts": list(self.alerts.values()),
            "sensors": [{"key": s, "label": SENSOR_LABEL[s], "unit": SENSOR_UNIT[s], **SENSOR_THRESHOLDS[s]} for s in SENSORS],
            "defaults": self.defaults(),
        }
        if reveal:
            out["scenario"] = self.scenario
        return out

    def series(self, machine_id: str, resolution: str) -> list[dict]:
        """Aggregated history for charts: 'mid' (1 min, 24 h) or 'long' (10 min, 7 days)."""
        m = self.machines.get(machine_id)
        if not m:
            return []
        return list(m.mid if resolution == "mid" else m.long)

    def defaults(self) -> dict:
        sensor_machines = set(D.table("sensor_readings")["machine_id"].unique())
        fleet = [m for m in D.records(D.table("machines")) if m["machine_id"] in sensor_machines]
        return {
            "fleet": [{"machine_id": m["machine_id"], "name": m["name"], "line": m["line"], "type": m["type"]} for m in fleet],
            "faults": [{"key": k, "label": v["label"], "sensor": v["sensor"]} for k, v in FAULTS.items()],
            "sensors": [{"key": s, "label": SENSOR_LABEL[s], "unit": SENSOR_UNIT[s], **SENSOR_THRESHOLDS[s]} for s in SENSORS],
        }

    def escalation_trigger(self, machine_id: str) -> dict:
        m = self.machines.get(machine_id)
        if not m:
            raise KeyError(machine_id)
        view = self._machine_view(m)
        alert = self.alerts.get(machine_id, {})
        sensor = alert.get("sensor") or (m.drift or {}).get("sensor") or view["top_sensor"] or (m.profile or {}).get("sensor")
        down = m.lifecycle in DOWN
        return {
            "source": "live", "machine_id": machine_id, "sensor": sensor,
            "value": view["values"].get(sensor) if view["values"].get(sensor) is not None else alert.get("value"),
            "risk": "critical" if (m.status == "critical" or down) else "high", "session_id": self.session_id,
            "rul_hours": 0 if m.lifecycle == "tripped" else (m.rul or {}).get("hours"),
            "live_snapshot": {"status": view["status"], "lifecycle": m.lifecycle, "health": view["health"],
                              "anomaly_score": view["score"], "work_order_id": m.work_order,
                              "down_since": _iso(m.down_since),
                              "drift": m.drift, "psi": m.psi, "rul": {k: v for k, v in (m.rul or {}).items() if k != "forecast"},
                              "recent_events": [e for e in self.events if e["machine_id"] == machine_id][-6:]},
        }

    # -- fan-out hook (overridden by the FastAPI backend) ------------------
    def _broadcast(self, payload: dict):
        pass
