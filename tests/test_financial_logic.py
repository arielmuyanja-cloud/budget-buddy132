import pytest

from app import _pct_change, compute_profitability_diagnosis, compute_forecast


def month(label, income, expense, categories=None):
    return {
        "label": label,
        "income": income,
        "expense": expense,
        "profit": income - expense,
        "margin": ((income - expense) / income * 100.0) if income > 0 else None,
        "expense_by_category": categories or {},
        "income_by_desc": {},
    }


def test_pct_change_returns_none_for_zero_baseline():
    assert _pct_change(1000, 0) is None


def test_profitability_diagnosis_does_not_emit_fake_percentage_for_zero_baseline():
    diagnosis = compute_profitability_diagnosis([
        month("Jul 2026", 0, 100),
        month("Aug 2026", 5000, 200),
    ])
    assert diagnosis["revenue_change_pct"] is None
    assert diagnosis["expense_change_pct"] == pytest.approx(100.0)


def test_forecast_exposes_source_months_for_small_sample_warning():
    forecast = compute_forecast([
        month("Jul 2026", 10000, 9000),
        month("Aug 2026", 12000, 11000),
    ])
    assert forecast["based_on_months"] == ["Jul 2026", "Aug 2026"]
    assert len(forecast["based_on_months"]) < 3


def test_forecast_uses_three_month_window_when_available():
    forecast = compute_forecast([
        month("Jun 2026", 10000, 8000),
        month("Jul 2026", 11000, 9000),
        month("Aug 2026", 12000, 10000),
    ])
    assert len(forecast["based_on_months"]) == 3
