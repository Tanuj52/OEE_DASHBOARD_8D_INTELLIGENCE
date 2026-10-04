"""
Prediction Engine for Predictive Maintenance.

Detects anomalies in sensor time-series data:
- Z-score based anomaly detection
- Trend analysis (linear regression on rolling window)
- Remaining Useful Life estimation (linear extrapolation to threshold)
"""

import numpy as np
import pandas as pd


# Thresholds for sensor alerts
SENSOR_THRESHOLDS = {
    "vibration_mm_s": {"warning": 3.5, "critical": 5.0, "direction": "upper"},
    "temp_c": {"warning": 55.0, "critical": 65.0, "direction": "upper"},
    "pressure_bar": {"warning": 4.0, "critical": 3.0, "direction": "lower"},
    "power_kw": {"warning": 16.0, "critical": 20.0, "direction": "upper"},
    "coolant_flow_lpm": {"warning": 3.0, "critical": 2.0, "direction": "lower"},
}

# The live stream also carries spindle speed (the ERP process records log it per order as speed_rpm,
# around 2,500 rpm); the historical sensor_readings table has no speed column, so it is live-only.
LIVE_THRESHOLDS = {**SENSOR_THRESHOLDS,
                   "spindle_rpm": {"warning": 2380.0, "critical": 2300.0, "direction": "lower"}}


def detect_anomalies(readings_df: pd.DataFrame, machine_id: str,
                     sensor_col: str, window: int = 50,
                     z_threshold: float = 2.5) -> dict:
    """Detect anomalies using z-score on a rolling window."""
    machine_data = readings_df[readings_df["machine_id"] == machine_id].copy()
    machine_data = machine_data.sort_values("timestamp")

    if len(machine_data) < window:
        return {"anomalies": [], "status": "insufficient_data"}

    values = machine_data[sensor_col].values.astype(float)
    rolling_mean = pd.Series(values).rolling(window).mean().values
    rolling_std = pd.Series(values).rolling(window).std().values

    anomalies = []
    for i in range(window, len(values)):
        if rolling_std[i] > 0:
            z = abs(values[i] - rolling_mean[i]) / rolling_std[i]
            if z > z_threshold:
                anomalies.append({
                    "index": i,
                    "timestamp": str(machine_data.iloc[i]["timestamp"]),
                    "value": round(float(values[i]), 3),
                    "z_score": round(float(z), 2),
                    "rolling_mean": round(float(rolling_mean[i]), 3),
                })

    return {
        "machine_id": machine_id,
        "sensor": sensor_col,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[-10:],  # last 10
        "status": "anomalies_detected" if anomalies else "normal",
    }


def analyze_trend(readings_df: pd.DataFrame, machine_id: str,
                  sensor_col: str, window_days: int = 14) -> dict:
    """Analyze trend using linear regression on recent data."""
    machine_data = readings_df[readings_df["machine_id"] == machine_id].copy()
    machine_data["timestamp"] = pd.to_datetime(machine_data["timestamp"])
    machine_data = machine_data.sort_values("timestamp")

    if len(machine_data) < 10:
        return {"trend": "insufficient_data"}

    cutoff = machine_data["timestamp"].max() - pd.Timedelta(days=window_days)
    recent = machine_data[machine_data["timestamp"] >= cutoff]

    if len(recent) < 5:
        return {"trend": "insufficient_data"}

    values = recent[sensor_col].values.astype(float)
    x = np.arange(len(values), dtype=float)

    # Linear regression
    slope, intercept = np.polyfit(x, values, 1)
    r_squared = 1 - np.sum((values - (slope * x + intercept))**2) / np.sum((values - np.mean(values))**2)

    current_value = float(values[-1])
    rate_per_day = slope * (len(values) / window_days) if window_days > 0 else 0

    # Classify trend
    threshold_info = SENSOR_THRESHOLDS.get(sensor_col, {})
    if abs(slope) < 0.001:
        trend = "stable"
    elif slope > 0:
        trend = "increasing"
    else:
        trend = "decreasing"

    return {
        "machine_id": machine_id,
        "sensor": sensor_col,
        "trend": trend,
        "slope": round(float(slope), 6),
        "rate_per_day": round(float(rate_per_day), 4),
        "current_value": round(current_value, 3),
        "r_squared": round(float(r_squared), 3),
        "window_days": window_days,
        "data_points": len(recent),
    }


def estimate_rul(readings_df: pd.DataFrame, machine_id: str,
                 sensor_col: str, window_days: int = 14) -> dict:
    """Estimate Remaining Useful Life by extrapolating trend to threshold."""
    trend = analyze_trend(readings_df, machine_id, sensor_col, window_days)

    if trend.get("trend") == "insufficient_data":
        return {"rul_days": None, "status": "insufficient_data"}

    threshold_info = SENSOR_THRESHOLDS.get(sensor_col)
    if not threshold_info:
        return {"rul_days": None, "status": "no_threshold_defined"}

    current = trend["current_value"]
    rate = trend["rate_per_day"]
    direction = threshold_info["direction"]
    critical = threshold_info["critical"]
    warning = threshold_info["warning"]

    # Calculate days to critical threshold
    if direction == "upper":
        if rate <= 0:
            rul_days = None  # not degrading
            risk = "low"
        else:
            rul_days = (critical - current) / rate if rate > 0 else None
            if current >= critical:
                risk = "critical"
            elif current >= warning:
                risk = "high"
            else:
                risk = "medium" if (rul_days and rul_days < 30) else "low"
    else:  # lower
        if rate >= 0:
            rul_days = None
            risk = "low"
        else:
            rul_days = (current - critical) / abs(rate) if rate < 0 else None
            if current <= critical:
                risk = "critical"
            elif current <= warning:
                risk = "high"
            else:
                risk = "medium" if (rul_days and rul_days < 30) else "low"

    # Recommendation
    if risk == "critical":
        recommendation = "IMMEDIATE: Trigger 8D investigation. Machine producing defective parts."
    elif risk == "high":
        recommendation = "URGENT: Schedule maintenance within 48 hours."
    elif risk == "medium":
        recommendation = "PLAN: Schedule maintenance within 2 weeks."
    else:
        recommendation = "MONITOR: No immediate action needed."

    return {
        "machine_id": machine_id,
        "sensor": sensor_col,
        "current_value": trend["current_value"],
        "warning_threshold": warning,
        "critical_threshold": critical,
        "trend": trend["trend"],
        "rate_per_day": trend["rate_per_day"],
        "rul_days": round(rul_days, 1) if rul_days is not None else None,
        "risk_level": risk,
        "recommendation": recommendation,
        "trigger_8d": risk in ("critical", "high"),
    }


def scan_all_machines(readings_df: pd.DataFrame) -> list[dict]:
    """Scan all machines across all sensor types and return alerts."""
    machines = readings_df["machine_id"].unique()
    alerts = []

    for machine_id in machines:
        for sensor_col in SENSOR_THRESHOLDS:
            if sensor_col not in readings_df.columns:
                continue
            rul = estimate_rul(readings_df, machine_id, sensor_col)
            if rul.get("risk_level") in ("high", "critical"):
                alerts.append(rul)

    alerts.sort(key=lambda x: {"critical": 0, "high": 1}.get(x.get("risk_level", ""), 2))
    return alerts
