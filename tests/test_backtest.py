"""
Unit tests for backtest.py -- all pure, no network, mirrors the deck's
disclosed accrued-return methodology (page 6): equal-weighted top-N
basket, same capital redeployed monthly, returns summed not compounded.
"""

from datetime import date

import pandas as pd
import pytest

import backtest


def test_month_starts_between_spans_year_boundary():
    months = backtest.month_starts_between(date(2025, 11, 15), date(2026, 2, 3))
    assert months == [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


def test_compute_monthly_basket_return_equal_weighted():
    cm_history = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB", "BBB"],
            "trade_date": pd.to_datetime(["2026-04-01", "2026-04-30", "2026-04-01", "2026-04-30"]),
            "close_price": [100, 110, 200, 180],  # AAA +10%, BBB -10%
        }
    )
    r = backtest.compute_monthly_basket_return(
        ["AAA", "BBB"], cm_history, date(2026, 4, 1), date(2026, 4, 30)
    )
    assert r == pytest.approx(0.0)  # +10 and -10 average to 0


def test_compute_monthly_basket_return_drops_missing_symbol():
    cm_history = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "trade_date": pd.to_datetime(["2026-04-01", "2026-04-30"]),
            "close_price": [100, 120],
        }
    )
    r = backtest.compute_monthly_basket_return(
        ["AAA", "MISSING"], cm_history, date(2026, 4, 1), date(2026, 4, 30)
    )
    assert r == pytest.approx(20.0)  # only AAA counted, MISSING silently dropped


def test_compute_monthly_basket_return_none_when_nothing_priced():
    cm_history = pd.DataFrame(columns=["symbol", "trade_date", "close_price"])
    r = backtest.compute_monthly_basket_return(["AAA"], cm_history, date(2026, 4, 1), date(2026, 4, 30))
    assert r is None


def test_compute_accrued_return_sums_not_compounds():
    # Sum, not compound: 10 + 10 = 20, NOT (1.1*1.1-1)*100 = 21
    assert backtest.compute_accrued_return([10.0, 10.0]) == pytest.approx(20.0)
    assert backtest.compute_accrued_return([10.0, None, -5.0]) == pytest.approx(5.0)


def test_run_backtest_end_to_end_with_benchmark():
    month_starts = [date(2026, 4, 1), date(2026, 5, 1)]

    # Portfolio always holds AAA+BBB; benchmark is a single flat index.
    def ranking_fn(month_start):
        return ["AAA", "BBB"]

    price_table = {
        (date(2026, 4, 1), date(2026, 4, 30)): {"AAA": (100, 120), "BBB": (200, 220), "BENCHMARK": (1000, 1010)},
        (date(2026, 5, 1), date(2026, 5, 31)): {"AAA": (120, 108), "BBB": (220, 210), "BENCHMARK": (1010, 1005)},
    }

    def cm_history_fn(month_start, month_end):
        prices = price_table[(month_start, month_end)]
        rows = []
        for sym, (entry, exit_) in prices.items():
            rows.append({"symbol": sym, "trade_date": pd.Timestamp(month_start), "close_price": entry})
            rows.append({"symbol": sym, "trade_date": pd.Timestamp(month_end), "close_price": exit_})
        return pd.DataFrame(rows)

    result = backtest.run_backtest(month_starts, ranking_fn, cm_history_fn, benchmark_symbol="BENCHMARK")

    # April: AAA +20%, BBB +10% -> avg +15%. May: AAA -10%, BBB -4.545% -> avg -7.27%
    assert result["monthly"][0]["portfolio_return_pct"] == pytest.approx(15.0)
    assert result["monthly"][1]["portfolio_return_pct"] == pytest.approx(-7.2727, rel=1e-3)
    assert result["portfolio_accrued_return_pct"] == pytest.approx(15.0 - 7.2727, rel=1e-3)

    assert result["monthly"][0]["benchmark_return_pct"] == pytest.approx(1.0)
    assert result["monthly"][1]["benchmark_return_pct"] == pytest.approx(-0.495, rel=1e-2)
