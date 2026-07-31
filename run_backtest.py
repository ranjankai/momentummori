#!/usr/bin/env python3
"""
CLI: run a real backtest over NSE data and print month-by-month +
accrued returns, using the same measurement methodology as page 6 of
the Altcase Momentum Leaders deck (equal-weighted top-N basket,
same capital redeployed monthly, returns summed not compounded).

This does NOT reproduce Altcase's exact stock selection -- see
backtest.py's module docstring for why an exact match to their
+61.2% figure isn't expected even with correct data.

Usage:
  python run_backtest.py --start 2025-04-01 --end 2026-04-30
  python run_backtest.py --start 2025-04-01 --end 2026-04-30 --top-n 10 \
      --benchmark-csv nifty50_tri.csv

--benchmark-csv expects a CSV with columns: date,close (e.g. exported
from niftyindices.com's NIFTY 50 TRI historical data page) -- NIFTY
index levels aren't in NSE's equity bhavcopy, so there's no automatic
fetch for them.
"""

import argparse
import logging
import logging.handlers
import os
from datetime import date, timedelta

import pandas as pd

import backtest
import config
import nse_client
import pipeline
import scoring

logger = logging.getLogger("momentum_tracker.run_backtest")


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=5)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger("momentum_tracker")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
        root.addHandler(console)


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def make_ranking_fn(top_n: int):
    def _ranking_fn(month_start: date):
        as_of = month_start - timedelta(days=1)  # rank using only prior data -- no look-ahead
        try:
            result = pipeline.run(as_of=as_of, persist_cache=False)
        except RuntimeError as exc:
            logger.error("Ranking failed for %s: %s", month_start, exc)
            return []
        return [r["symbol"] for r in result["rankings"][:top_n]]
    return _ranking_fn


def make_cm_history_fn(benchmark_df: pd.DataFrame | None):
    def _cm_history_fn(month_start: date, month_end: date):
        frames = []
        d = month_start
        while d <= month_end:
            if d.weekday() < 5:
                try:
                    raw = nse_client.fetch_cm_bhavcopy(d)
                    norm = scoring.normalize_cm_columns(raw)
                    norm["trade_date"] = pd.Timestamp(d)
                    frames.append(norm)
                except nse_client.NseFetchError as exc:
                    logger.warning("No CM bhavcopy for %s: %s", d, exc)
            d += timedelta(days=1)

        history = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["symbol", "trade_date", "close_price"]
        )

        if benchmark_df is not None:
            window = benchmark_df[
                (benchmark_df["trade_date"] >= pd.Timestamp(month_start))
                & (benchmark_df["trade_date"] <= pd.Timestamp(month_end))
            ].copy()
            window["symbol"] = "BENCHMARK"
            history = pd.concat([history, window[["symbol", "trade_date", "close_price"]]], ignore_index=True)

        return history
    return _cm_history_fn


def load_benchmark_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError(f"{path} must have 'date' and 'close' columns, got {list(df.columns)}")
    df["trade_date"] = pd.to_datetime(df["date"])
    df["close_price"] = df["close"].astype(float)
    return df[["trade_date", "close_price"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, type=parse_date, help="YYYY-MM-DD, first month included")
    parser.add_argument("--end", required=True, type=parse_date, help="YYYY-MM-DD, last month included")
    parser.add_argument("--top-n", type=int, default=config.PORTFOLIO_SIZE)
    parser.add_argument("--benchmark-csv", default=None, help="CSV with date,close columns")
    args = parser.parse_args()

    setup_logging()

    benchmark_df = load_benchmark_csv(args.benchmark_csv) if args.benchmark_csv else None
    benchmark_symbol = "BENCHMARK" if benchmark_df is not None else None

    month_starts = backtest.month_starts_between(args.start, args.end)
    result = backtest.run_backtest(
        month_starts,
        ranking_fn=make_ranking_fn(args.top_n),
        cm_history_fn=make_cm_history_fn(benchmark_df),
        benchmark_symbol=benchmark_symbol,
    )

    print(f"\n{'Month':<10}{'Portfolio %':>14}{'Benchmark %':>14}   Symbols")
    for m in result["monthly"]:
        p = f"{m['portfolio_return_pct']:.1f}" if m["portfolio_return_pct"] is not None else "n/a"
        b = f"{m['benchmark_return_pct']:.1f}" if m.get("benchmark_return_pct") is not None else "n/a"
        print(f"{m['month']:<10}{p:>14}{b:>14}   {','.join(m['symbols'])}")

    print(f"\nPortfolio accrued return: {result['portfolio_accrued_return_pct']:.1f}%")
    if benchmark_symbol:
        print(f"Benchmark accrued return: {result['benchmark_accrued_return_pct']:.1f}%")
    print(
        "\nReminder: this uses this tool's own ranking (config.SIGNAL_WEIGHTS), "
        "not Altcase's undisclosed exact formula -- treat as a methodology "
        "sanity-check, not a reconciliation of the deck's +61.2% figure."
    )


if __name__ == "__main__":
    main()
