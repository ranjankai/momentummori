"""
Scoring engine: turns raw bhavcopy data into the four-signal composite
score and ranking described in the Altcase Momentum Leaders deck
(derivatives rollover %, cost of carry, price momentum, volume trend).

All functions operate on plain pandas DataFrames so they can be tested
against synthetic data (see tests/test_scoring.py) independently of the
NSE network client.
"""

import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger("momentum_tracker.scoring")


def normalize_fo_columns(fo_df: pd.DataFrame) -> pd.DataFrame:
    """Standardise NSE F&O bhavcopy columns to lowercase snake_case."""
    df = fo_df.rename(
        columns={
            "TckrSymb": "symbol",
            "FinInstrmTp": "instrument_type",
            "XpryDt": "expiry_date",
            "OpnIntrst": "open_interest",
            "ChngInOpnIntrst": "change_in_oi",
            "SttlmPric": "settlement_price",
            "TtlTradgVol": "volume",
        }
    ).copy()
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")
    return df


def normalize_cm_columns(cm_df: pd.DataFrame) -> pd.DataFrame:
    """Standardise NSE cash-market bhavcopy columns to lowercase snake_case."""
    df = cm_df.rename(
        columns={
            "TckrSymb": "symbol",
            "ClsPric": "close_price",
            "TtlTradgVol": "volume",
            "TradDt": "trade_date",
        }
    ).copy()
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    return df


def compute_rollover_pct(fo_today: pd.DataFrame, as_of, symbols) -> pd.Series:
    """
    Rollover % per symbol = next-month futures OI / (near-month + next-month OI).
    Only meaningful within the rollover window near expiry; callers should
    interpret low values outside that window with that caveat.
    """
    futs = fo_today[fo_today["instrument_type"].isin(["FUTSTK", "STF"])].copy()
    if futs.empty:
        return pd.Series(np.nan, index=symbols)

    results = {}
    for sym, group in futs.groupby("symbol"):
        g = group.sort_values("expiry_date")
        if len(g) < 2:
            results[sym] = np.nan
            continue
        near_oi = g.iloc[0]["open_interest"]
        next_oi = g.iloc[1]["open_interest"]
        denom = near_oi + next_oi
        results[sym] = (next_oi / denom * 100) if denom > 0 else np.nan

    return pd.Series(results).reindex(symbols)


def compute_cost_of_carry(
    fo_today: pd.DataFrame, spot_prices: pd.Series, symbols
) -> pd.Series:
    """
    Annualised cost of carry per symbol using the near-month future:
    (futures_settlement - spot) / spot * (365 / days_to_expiry) * 100
    """
    futs = fo_today[fo_today["instrument_type"].isin(["FUTSTK", "STF"])].copy()
    if futs.empty:
        return pd.Series(np.nan, index=symbols)

    as_of = futs["expiry_date"].min()  # placeholder; overridden by caller if needed
    results = {}
    for sym, group in futs.groupby("symbol"):
        near = group.sort_values("expiry_date").iloc[0]
        spot = spot_prices.get(sym, np.nan)
        expiry = near["expiry_date"]
        trade_date = near.get("trade_date", pd.NaT)
        days_to_expiry = (expiry - trade_date).days if pd.notna(trade_date) else np.nan
        if not spot or spot <= 0 or not days_to_expiry or days_to_expiry <= 0:
            results[sym] = np.nan
            continue
        results[sym] = (
            (near["settlement_price"] - spot) / spot * (365 / days_to_expiry) * 100
        )

    return pd.Series(results).reindex(symbols)


def compute_price_momentum(cm_history: pd.DataFrame, symbols) -> pd.Series:
    """
    Price momentum per symbol = % change in close price over the lookback
    window (config.PRICE_MOMENTUM_LOOKBACK_DAYS trading days).
    cm_history must have columns: symbol, trade_date, close_price.
    """
    results = {}
    for sym, group in cm_history.groupby("symbol"):
        g = group.sort_values("trade_date")
        if len(g) < 2:
            results[sym] = np.nan
            continue
        first_close = g.iloc[0]["close_price"]
        last_close = g.iloc[-1]["close_price"]
        results[sym] = (
            (last_close - first_close) / first_close * 100 if first_close else np.nan
        )
    return pd.Series(results).reindex(symbols)


def compute_volume_trend(cm_history: pd.DataFrame, symbols) -> pd.Series:
    """
    Volume trend per symbol = recent-half average volume / earlier-half
    average volume (>1 means participation is rising).
    """
    results = {}
    for sym, group in cm_history.groupby("symbol"):
        g = group.sort_values("trade_date")
        if len(g) < 4:
            results[sym] = np.nan
            continue
        mid = len(g) // 2
        earlier_avg = g.iloc[:mid]["volume"].mean()
        recent_avg = g.iloc[mid:]["volume"].mean()
        results[sym] = (recent_avg / earlier_avg) if earlier_avg else np.nan
    return pd.Series(results).reindex(symbols)


def _percentile_rank(series: pd.Series) -> pd.Series:
    """Rank a signal to 0-100 percentile, NaNs kept as NaN (excluded, not zeroed)."""
    return series.rank(pct=True, na_option="keep") * 100


def build_composite_scores(
    rollover_pct: pd.Series,
    cost_of_carry: pd.Series,
    price_momentum: pd.Series,
    volume_trend: pd.Series,
) -> pd.DataFrame:
    """
    Combine the four signals into a single composite score using
    percentile ranking (so signals on different scales are comparable)
    and the weights in config.SIGNAL_WEIGHTS.
    """
    weights = config.SIGNAL_WEIGHTS
    frame = pd.DataFrame(
        {
            "rollover_pct": rollover_pct,
            "cost_of_carry": cost_of_carry,
            "price_momentum": price_momentum,
            "volume_trend": volume_trend,
        }
    )

    ranked = pd.DataFrame(index=frame.index)
    for col in frame.columns:
        ranked[col + "_pctile"] = _percentile_rank(frame[col])

    frame["composite_score"] = (
        ranked["rollover_pct_pctile"] * weights["rollover_pct"]
        + ranked["cost_of_carry_pctile"] * weights["cost_of_carry"]
        + ranked["price_momentum_pctile"] * weights["price_momentum"]
        + ranked["volume_trend_pctile"] * weights["volume_trend"]
    )
    return frame


def rank_universe(
    rollover_pct: pd.Series,
    cost_of_carry: pd.Series,
    price_momentum: pd.Series,
    volume_trend: pd.Series,
    top_n: int = None,
) -> pd.DataFrame:
    """Return the top_n symbols by composite score, sorted descending."""
    top_n = top_n or config.PORTFOLIO_SIZE
    scored = build_composite_scores(
        rollover_pct, cost_of_carry, price_momentum, volume_trend
    )
    scored = scored.dropna(subset=["composite_score"])
    ranked = scored.sort_values("composite_score", ascending=False)
    result = ranked.head(top_n).reset_index().rename(columns={"index": "symbol"})
    result.insert(0, "rank", range(1, len(result) + 1))
    result["weight_pct"] = min(
        config.MAX_SINGLE_STOCK_WEIGHT_PCT, 100 / top_n if top_n else 0
    )
    return result
