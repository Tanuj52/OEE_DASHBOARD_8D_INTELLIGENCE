"""
COPQ (Cost of Poor Quality) Calculator.

Calculates internal failure costs, external failure costs, and investigation costs.
"""


def calculate_copq(
    scrap_units: int = 0,
    unit_cost: float = 0.0,
    rework_hours: float = 0.0,
    rework_rate_per_hour: float = 75.0,
    sorting_hours: float = 0.0,
    sorting_rate_per_hour: float = 45.0,
    downtime_hours: float = 0.0,
    downtime_cost_per_hour: float = 500.0,
    warranty_claims: int = 0,
    warranty_cost_per_claim: float = 200.0,
    returns_units: int = 0,
    return_processing_cost: float = 50.0,
    expedited_shipping_cost: float = 0.0,
    customer_penalty: float = 0.0,
    investigation_hours: float = 0.0,
    engineering_rate_per_hour: float = 120.0,
) -> dict:
    """Calculate full COPQ breakdown."""

    # Internal failure costs
    scrap_cost = scrap_units * unit_cost
    rework_cost = rework_hours * rework_rate_per_hour
    sorting_cost = sorting_hours * sorting_rate_per_hour
    downtime_cost = downtime_hours * downtime_cost_per_hour
    internal_total = scrap_cost + rework_cost + sorting_cost + downtime_cost

    # External failure costs
    warranty_cost = warranty_claims * warranty_cost_per_claim
    return_cost = returns_units * return_processing_cost
    external_total = warranty_cost + return_cost + expedited_shipping_cost + customer_penalty

    # Investigation costs
    investigation_cost = investigation_hours * engineering_rate_per_hour

    total_copq = internal_total + external_total + investigation_cost

    return {
        "internal_failure": {
            "scrap": round(scrap_cost, 2),
            "rework": round(rework_cost, 2),
            "sorting": round(sorting_cost, 2),
            "downtime": round(downtime_cost, 2),
            "subtotal": round(internal_total, 2),
        },
        "external_failure": {
            "warranty": round(warranty_cost, 2),
            "returns": round(return_cost, 2),
            "expedited_shipping": round(expedited_shipping_cost, 2),
            "customer_penalty": round(customer_penalty, 2),
            "subtotal": round(external_total, 2),
        },
        "investigation": {
            "engineering_hours": investigation_hours,
            "cost": round(investigation_cost, 2),
        },
        "total_copq": round(total_copq, 2),
        "breakdown_pct": {
            "internal": round(internal_total / total_copq * 100, 1) if total_copq > 0 else 0,
            "external": round(external_total / total_copq * 100, 1) if total_copq > 0 else 0,
            "investigation": round(investigation_cost / total_copq * 100, 1) if total_copq > 0 else 0,
        },
    }
