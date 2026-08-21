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


def test_forecast_marks_two_month_history_as_low_confidence():
    forecast = compute_forecast([
        month("Jul 2026", 10000, 9000),
        month("Aug 2026", 12000, 11000),
    ])
    assert forecast["sample_months"] == 2
    assert forecast["confidence"] == "LOW"
    assert forecast["small_sample"] is True


def test_forecast_has_higher_confidence_with_three_or_more_months():
    forecast = compute_forecast([
        month("Jun 2026", 10000, 8000),
        month("Jul 2026", 11000, 9000),
        month("Aug 2026", 12000, 10000),
    ])
    assert forecast["sample_months"] == 3
    assert forecast["small_sample"] is False
