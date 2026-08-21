"""Safe 90-day forecast replacement for the legacy compounding forecast.

The previous forecast compounded a percentage change derived from the first and
last month. When the first month was very small, that multiplier could explode
into billions or trillions. This module uses a bounded linear trend over the
recent months instead: no exponential compounding is applied.
"""


def safe_compute_forecast(months):
    if not months:
        return None

    window = months[-3:] if len(months) >= 3 else months
    n = len(window)
    avg_revenue = sum(float(m.get("income", 0.0) or 0.0) for m in window) / n
    avg_expense = sum(float(m.get("expense", 0.0) or 0.0) for m in window) / n

    def project(key):
        values = [float(m.get(key, 0.0) or 0.0) for m in window]
        mean_x = (n - 1) / 2.0
        mean_y = sum(values) / n
        denominator = sum((i - mean_x) ** 2 for i in range(n))
        slope = (
            sum((i - mean_x) * (value - mean_y) for i, value in enumerate(values))
            / denominator
            if denominator
            else 0.0
        )
        return [max(0.0, values[-1] + slope * step) for step in (1, 2, 3)]

    projected_revenue_months = project("income")
    projected_expense_months = project("expense")
    projected_revenue = sum(projected_revenue_months)
    projected_expense = sum(projected_expense_months)
    flat_run_rate_profit = (avg_revenue - avg_expense) * 3

    return {
        "based_on_months": [m["label"] for m in window],
        "projected_revenue": projected_revenue,
        "projected_expense": projected_expense,
        "projected_profit": projected_revenue - projected_expense,
        "profit_delta_vs_flat": (projected_revenue - projected_expense) - flat_run_rate_profit,
        "forecast_method": "linear trend over up to 3 recent months; no compounding multiplier",
    }
