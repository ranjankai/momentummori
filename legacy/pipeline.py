"""
End-to-end pipeline: fetches bhavcopy data across the lookback window,
computes the four signals, ranks the universe, and returns a JSON-ready
result. This is the single entry point the Flask app calls.
"""

import json
import logging
from datetime import date, timedelta

import pandas as pd

import config
import nse_client
import scoring

logger = logging.getLogger("momentum_tracker.pipeline")


def load_universe() -> list:
    df = pd.read_csv(config.UNIVERSE_FILE)
    symbols = sorted(set(df["symbol"].str.strip().str.upper()))
    return symbols


def load_sector_map() -> dict:
    """
    Load symbol -> sector for the sector concentration cap. Missing
    file degrades gracefully (logs a warning, returns {}) rather than
    crashing the pipeline -- scoring.rank_universe treats an empty/None
    map as "no sector cap", same as before this feature existed.
    """
    import os

    if not os.path.exists(config.SECTOR_MAP_FILE):
        logger.warning(
            "Sector map file not found at %s -- running without the sector "
            "concentration cap.", config.SECTOR_MAP_FILE,
        )
        return {}
    df = pd.read_csv(config.SECTOR_MAP_FILE)
    return dict(zip(df["symbol"].str.strip().str.upper(), df["sector"]))


def _trading_days_back(as_of: date, n: int) -> list:
    """Naive calendar-day walk back skipping weekends; NSE holidays are
    tolerated by nse_client's 404 handling (caller should skip failed days)."""
    days = []
    d = as_of
    while len(days) < n:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def run(as_of: date = None, persist_cache: bool = True) -> dict:
    """
    Run the full pipeline for a single as_of date.

    persist_cache=False skips writing data/latest.json — used by
    backtest.py so a multi-month backtest loop doesn't clobber the
    live dashboard's cached "latest" result on every historical month.
    """
    as_of = as_of or date.today()
    symbols = load_universe()
    logger.info("Running pipeline for %s over %d symbols", as_of, len(symbols))

    history_days = _trading_days_back(as_of, config.PRICE_MOMENTUM_LOOKBACK_DAYS)

    cm_frames = []
    errors = []
    for d in history_days:
        try:
            raw = nse_client.fetch_cm_bhavcopy(d)
            norm = scoring.normalize_cm_columns(raw)
            norm["trade_date"] = pd.Timestamp(d)
            cm_frames.append(norm)
        except nse_client.NseFetchError as exc:
            logger.warning("Skipping %s (CM bhavcopy unavailable): %s", d, exc)
            errors.append(str(exc))

    if not cm_frames:
        raise RuntimeError(
            "No cash-market bhavcopy data could be fetched for the requested "
            "window. See logs/app.log for details."
        )

    cm_history = pd.concat(cm_frames, ignore_index=True)
    cm_history = cm_history[cm_history["symbol"].isin(symbols)]

    latest_date = cm_history["trade_date"].max()
    cm_today = cm_history[cm_history["trade_date"] == latest_date]
    spot_prices = cm_today.set_index("symbol")["close_price"]

    # Use latest_date (the last date we actually got cash-market data for)
    # rather than the raw as_of -- as_of can be a weekend/holiday with no
    # bhavcopy at all (e.g. ranking "as of" the day before a month start
    # that happens to be a market holiday), while latest_date is
    # guaranteed to be a real trading day since cm_frames wasn't empty.
    fo_fetch_date = latest_date.date()
    fo_raw = nse_client.fetch_fo_bhavcopy(fo_fetch_date)
    fo_today = scoring.normalize_fo_columns(fo_raw)
    fo_today = fo_today[fo_today["symbol"].isin(symbols)]
    if "trade_date" not in fo_today.columns:
        fo_today["trade_date"] = pd.Timestamp(latest_date)

    rollover_pct = scoring.compute_rollover_pct(fo_today, latest_date, symbols)
    cost_of_carry = scoring.compute_cost_of_carry(fo_today, spot_prices, symbols)
    price_momentum = scoring.compute_price_momentum(cm_history, symbols)
    volume_trend = scoring.compute_volume_trend(cm_history, symbols)
    volatility = scoring.compute_volatility(cm_history, symbols)
    sector_map = load_sector_map()

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend,
        volatility=volatility, sector_map=sector_map,
    )

    result = {
        "as_of": str(as_of),
        "latest_bhavcopy_date": str(latest_date.date()) if pd.notna(latest_date) else None,
        "universe_size": len(symbols),
        "days_with_data": len(cm_frames),
        "fetch_warnings": errors,
        "rankings": json.loads(ranked.to_json(orient="records")),
    }

    if persist_cache:
        os_makedirs_and_write(result)
    return result


def os_makedirs_and_write(result: dict) -> None:
    import os

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.LATEST_RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2)


def load_latest_cached() -> dict | None:
    import os

    if not os.path.exists(config.LATEST_RESULT_FILE):
        return None
    with open(config.LATEST_RESULT_FILE) as f:
        return json.load(f)
