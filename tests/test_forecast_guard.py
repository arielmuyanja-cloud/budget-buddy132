import unittest

from forecast_guard import safe_compute_forecast


class ForecastGuardTests(unittest.TestCase):
    def test_empty_history_returns_none(self):
        self.assertIsNone(safe_compute_forecast([]))

    def test_forecast_does_not_compound_exponentially(self):
        months = [
            {"label": "Jan 2026", "income": 100000, "expense": 100},
            {"label": "Feb 2026", "income": 100200, "expense": 10000},
            {"label": "Mar 2026", "income": 100400, "expense": 20000},
        ]
        result = safe_compute_forecast(months)
        self.assertIsNotNone(result)
        self.assertLess(result["projected_expense"], 100000)
        self.assertLess(result["projected_expense"], 10_000_000)
        self.assertEqual(result["forecast_method"], "linear trend over up to 3 recent months; no compounding multiplier")

    def test_negative_linear_projection_is_clamped_to_zero(self):
        months = [
            {"label": "Jan 2026", "income": 1000, "expense": 3000},
            {"label": "Feb 2026", "income": 1000, "expense": 2000},
            {"label": "Mar 2026", "income": 1000, "expense": 1000},
        ]
        result = safe_compute_forecast(months)
        self.assertEqual(result["projected_expense"], 0.0)


if __name__ == "__main__":
    unittest.main()
