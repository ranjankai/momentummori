"""
Backtest mode: reproduces the *accrued-return methodology* described on
page 6 of the Altcase Momentum Leaders deck — the same capital is
deployed at the start of every month, each month's profit/loss is
"banked" (not reinvested), and the headline number is the simple SUM
of monthly returns, not a compounded CAGR.

Important: this reproduces the deck's *measurement* methodology, not
its *stock selection*. Altcase's exact composite formula, weights, and
rollover-window rules aren't public — this tool ranks stocks using the
weights in config.SIGNAL_WEIGHTS, which are reasonable defaults I
chose, not Altcase's. Do not expect the same top-10 list or the same
+61.2% figure; use this to sanity-check whether a similarly-built
momentum strategy shows a similarly-shaped result (beats the benchmark
most months, occasional sharp drawdown months, etc.), not to
reconcile the exact number.

The core functions here are pure (no network calls) so they can be
unit tested against synthetic data — see tests/test_backtest.py.
"""

import logging
from calendar import monthrange
from datetime import date

import pandas as pd

logger = logging.getLogger("momentum_tracker.backtest")


def month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def month_starts_between(start: date, end: date):
    """First-of-month dates from start's month through end's month, inclusive."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _first_and_last_close(cm_history: pd.DataFrame, symbol: str, start: date, end: date):
    g = cm_history[
        (cm_history["symbol"] == symbol)
        & (cm_history["trade_date"] >= pd.Timestamp(start))
        & (cm_history["trade_date"] <= pd.Timestamp(end))
    ].sort_values("trade_date")
    if g.empty:
        return None, None
    return float(g.iloc[0]["close_price"]), float(g.iloc[-1]["close_price"])


def compute_monthly_basket_return(symbols, cm_history: pd.DataFrame, start: date, end: date):
    """
    Equal-weighted % return of a fixed basket over one month:
    entry = first trading-day close in the window, exit = last.
    Symbols with no price data that month are dropped (and logged),
    not treated as 0% -- consistent with "position never opened".
    """
    leg_returns = []
    for sym in symbols:
        entry, exit_ = _first_and_last_close(cm_history, sym, start, end)
        if entry is None or exit_ is None or entry == 0:
            logger.warning("No price data for %s between %s and %s — excluded from that month", sym, start, end)
            continue
        leg_returns.append((exit_ - entry) / entry * 100)
    if not leg_returns:
        return None
    return sum(leg_returns) / len(leg_returns)


def compute_accrued_return(monthly_returns) -> float:
    """Sum (NOT compound) of monthly returns — matches the deck's disclosed methodology."""
    return sum(r for r in monthly_returns if r is not None)


def run_backtest(month_starts, ranking_fn, cm_history_fn, benchmark_symbol=None):
    """
    Generic, network-free backtest driver.

    ranking_fn(month_start) -> list[str] top-N symbols to hold that month,
        computed using only data available *before* month_start (caller's
        responsibility to avoid look-ahead bias).
    cm_history_fn(month_start, month_end) -> DataFrame[symbol, trade_date, close_price]
        covering prices for that month (portfolio symbols + benchmark_symbol
        if given).
    benchmark_symbol: optional single symbol/index code to compute the same
        monthly-return methodology on, for comparison.
    """
    monthly = []
    for month_start in month_starts:
        _, month_end = month_bounds(month_start.year, month_start.month)
        symbols = ranking_fn(month_start)
        cm_history = cm_history_fn(month_start, month_end)

        portfolio_return = compute_monthly_basket_return(symbols, cm_history, month_start, month_end)
        benchmark_return = None
        if benchmark_symbol:
            benchmark_return = compute_monthly_basket_return(
                [benchmark_symbol], cm_history, month_start, month_end
            )

        monthly.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "symbols": symbols,
                "portfolio_return_pct": portfolio_return,
                "benchmark_return_pct": benchmark_return,
            }
        )

    result = {
        "monthly": monthly,
        "portfolio_accrued_return_pct": compute_accrued_return(
            [m["portfolio_return_pct"] for m in monthly]
        ),
    }
    if benchmark_symbol:
        result["benchmark_accrued_return_pct"] = compute_accrued_return(
            [m["benchmark_return_pct"] for m in monthly]
        )
    return result
