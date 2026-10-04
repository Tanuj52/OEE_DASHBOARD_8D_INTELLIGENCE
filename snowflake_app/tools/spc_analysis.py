"""
SPC (Statistical Process Control) Analysis Tool.

Calculates:
- Control chart data (I-MR or X-bar/R)
- Cp, Cpk, Pp, Ppk indices
- Out-of-control detection (Nelson rules 1-4)
- Process capability verdict
"""

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats


def calculate_capability(values: np.ndarray, usl: float, lsl: float) -> dict:
    """Calculate Cp, Cpk, Pp, Ppk for a set of measurements."""
    mean = np.mean(values)
    std = np.std(values, ddof=1)

    if std == 0 or np.isnan(std):
        return {"cp": None, "cpk": None, "pp": None, "ppk": None,
                "mean": float(mean), "std": 0, "verdict": "Insufficient variation"}

    cp = (usl - lsl) / (6 * std)
    cpu = (usl - mean) / (3 * std)
    cpl = (mean - lsl) / (3 * std)
    cpk = min(cpu, cpl)

    # Pp/Ppk use overall std (same as Cp/Cpk for single subgroup)
    pp = cp
    ppk = cpk

    if cpk >= 1.67:
        verdict = "Excellent (Six Sigma capable)"
    elif cpk >= 1.33:
        verdict = "Good (meets automotive requirements)"
    elif cpk >= 1.0:
        verdict = "Marginal (minimum acceptable)"
    else:
        verdict = "INCAPABLE (below minimum, action required)"

    return {
        "cp": round(cp, 3),
        "cpk": round(cpk, 3),
        "pp": round(pp, 3),
        "ppk": round(ppk, 3),
        "mean": round(float(mean), 4),
        "std": round(float(std), 4),
        "usl": usl,
        "lsl": lsl,
        "sample_size": len(values),
        "verdict": verdict,
        "ppm_estimate": round(
            (1 - scipy_stats.norm.cdf(cpk * 3)) * 2 * 1_000_000, 1
        ) if cpk > 0 else 1_000_000,
    }


def control_chart_imr(values: np.ndarray) -> dict:
    """Individual and Moving Range (I-MR) control chart."""
    n = len(values)
    if n < 2:
        return {"error": "Need at least 2 data points"}

    individuals = values.astype(float)
    moving_ranges = np.abs(np.diff(individuals))

    x_bar = np.mean(individuals)
    mr_bar = np.mean(moving_ranges)

    # d2 constant for subgroup size 2
    d2 = 1.128
    sigma_est = mr_bar / d2

    ucl_i = x_bar + 3 * sigma_est
    lcl_i = x_bar - 3 * sigma_est
    ucl_mr = 3.267 * mr_bar  # D4 for n=2

    return {
        "type": "I-MR",
        "individuals": {
            "values": individuals.tolist(),
            "center_line": round(x_bar, 4),
            "ucl": round(ucl_i, 4),
            "lcl": round(lcl_i, 4),
        },
        "moving_range": {
            "values": moving_ranges.tolist(),
            "center_line": round(mr_bar, 4),
            "ucl": round(ucl_mr, 4),
            "lcl": 0,
        },
        "sigma_estimate": round(sigma_est, 4),
    }


def detect_nelson_rules(values: np.ndarray, ucl: float, lcl: float,
                        center: float) -> list[dict]:
    """Detect Nelson rule violations (rules 1-4)."""
    violations = []
    n = len(values)
    sigma = (ucl - center) / 3

    if sigma == 0:
        return violations

    # Rule 1: One point beyond 3 sigma
    for i, v in enumerate(values):
        if v > ucl or v < lcl:
            violations.append({
                "rule": 1,
                "description": "Point beyond 3-sigma control limit",
                "index": i,
                "value": round(float(v), 4),
            })

    # Rule 2: Nine consecutive points on same side of center
    if n >= 9:
        for i in range(n - 8):
            window = values[i:i+9]
            if all(w > center for w in window) or all(w < center for w in window):
                violations.append({
                    "rule": 2,
                    "description": "9 consecutive points on same side of center line",
                    "index": i,
                    "value": None,
                })
                break

    # Rule 3: Six consecutive points steadily increasing or decreasing
    if n >= 6:
        for i in range(n - 5):
            window = values[i:i+6]
            diffs = np.diff(window)
            if all(d > 0 for d in diffs) or all(d < 0 for d in diffs):
                violations.append({
                    "rule": 3,
                    "description": "6 consecutive points trending (monotonic increase/decrease)",
                    "index": i,
                    "value": None,
                })
                break

    # Rule 4: Two of three consecutive points beyond 2 sigma (same side)
    two_sigma_upper = center + 2 * sigma
    two_sigma_lower = center - 2 * sigma
    if n >= 3:
        for i in range(n - 2):
            window = values[i:i+3]
            above = sum(1 for w in window if w > two_sigma_upper)
            below = sum(1 for w in window if w < two_sigma_lower)
            if above >= 2 or below >= 2:
                violations.append({
                    "rule": 4,
                    "description": "2 of 3 consecutive points beyond 2-sigma",
                    "index": i,
                    "value": None,
                })
                break

    return violations


def run_spc_analysis(values: np.ndarray, usl: float, lsl: float) -> dict:
    """Complete SPC analysis: capability + control chart + Nelson rules."""
    capability = calculate_capability(values, usl, lsl)
    chart = control_chart_imr(values)
    violations = []

    if "individuals" in chart:
        violations = detect_nelson_rules(
            values,
            chart["individuals"]["ucl"],
            chart["individuals"]["lcl"],
            chart["individuals"]["center_line"],
        )

    return {
        "capability": capability,
        "control_chart": chart,
        "nelson_violations": violations,
        "out_of_control": len(violations) > 0,
        "summary": _build_summary(capability, violations),
    }


def _build_summary(capability: dict, violations: list) -> str:
    cpk = capability.get("cpk")
    if cpk is None:
        return "Insufficient data for SPC analysis."

    lines = [f"Process Capability: Cpk = {cpk} — {capability['verdict']}"]
    lines.append(f"Estimated PPM defective: {capability['ppm_estimate']}")

    if violations:
        lines.append(f"OUT OF CONTROL: {len(violations)} Nelson rule violation(s) detected:")
        for v in violations:
            lines.append(f"  - Rule {v['rule']}: {v['description']}")
    else:
        lines.append("Process is in statistical control (no Nelson rule violations).")

    return "\n".join(lines)
