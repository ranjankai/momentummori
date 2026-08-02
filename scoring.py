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
            "TtlTrfVal": "turnover",
        }
    ).copy()
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce")
    return df


def normalize_cm_columns(cm_df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardise NSE cash-market bhavcopy columns to lowercase snake_case.

    NSE lists some symbols under more than one series on the same day
    (e.g. IIFL trades under both "EQ" and "BL") -- if left in, that
    produces two rows per symbol per date, which breaks every downstream
    function that assumes one close price per symbol per day (observed
    live: pandas raises "truth value of a Series is ambiguous" deep in
    compute_cost_of_carry because spot_prices.get(symbol) returns more
    than one row). Keep only the primary "EQ" series when the raw
    SctySrs column is present, then defensively drop any remaining
    duplicate symbol/date rows.
    """
    df = cm_df.copy()
    if "SctySrs" in df.columns:
        eq_only = df[df["SctySrs"] == "EQ"]
        # Fall back to unfiltered if a symbol has no "EQ" row at all
        # (rare, but don't silently drop a whole symbol over it).
        missing_symbols = set(df["TckrSymb"]) - set(eq_only["TckrSymb"])
        if missing_symbols:
            df = pd.concat([eq_only, df[df["TckrSymb"].isin(missing_symbols)]], ignore_index=True)
        else:
            df = eq_only

    df = df.rename(
        columns={
            "TckrSymb": "symbol",
            "ClsPric": "close_price",
            "TtlTradgVol": "volume",
        "TtlTrfVal": "turnover",
            "TradDt": "trade_date",
        }
    ).copy()
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")

    dedup_keys = [c for c in ("symbol", "trade_date") if c in df.columns]
    if dedup_keys:
        before = len(df)
        df = df.drop_duplicates(subset=dedup_keys, keep="first")
        if len(df) < before:
            logger.warning(
                "Dropped %d duplicate symbol/date rows from CM bhavcopy after series filtering",
                before - len(df),
            )
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

    On a monthly F&O expiry day itself, the literal nearest-listed
    contract expires that same day (0 days to expiry) for every stock
    at once -- annualising a 0-day cost of carry is meaningless, so
    those contracts are skipped in favour of the next one out. This
    matters more than it looks: since it happens simultaneously across
    the whole universe, leaving it unfixed made every symbol's cost of
    carry NaN on expiry days, which zeroed out the entire composite
    score and produced zero ranked stocks for that day (caught via a
    real backtest run landing exactly on 31 Jul 2025, a monthly expiry).
    """
    futs = fo_today[fo_today["instrument_type"].isin(["FUTSTK", "STF"])].copy()
    if futs.empty:
        return pd.Series(np.nan, index=symbols)

    results = {}
    for sym, group in futs.groupby("symbol"):
        g = group.sort_values("expiry_date")
        # Drop contracts that expire on or before their own trade date --
        # i.e. today's expiry itself -- before picking the "near" one.
        tradeable = g[g["expiry_date"] > g["trade_date"]] if "trade_date" in g.columns else g
        if tradeable.empty:
            results[sym] = np.nan
            continue
        near = tradeable.iloc[0]
        spot = spot_prices.get(sym, np.nan)
        if isinstance(spot, pd.Series):
            # Defensive: spot_prices should be one row per symbol (see
            # normalize_cm_columns' series dedup), but if a duplicate
            # slips through anyway, don't crash the whole pipeline over
            # one bad symbol -- take the first value and move on.
            logger.warning("Multiple spot prices found for %s, using the first", sym)
            spot = spot.iloc[0] if not spot.empty else np.nan
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


def compute_volatility(cm_history: pd.DataFrame, symbols) -> pd.Series:
    """
    Realised volatility per symbol = std dev of daily %% price changes
    over the lookback window. Added after a real backtest run showed
    this tool's worst drawdown months (July 2025, March 2026) both
    picked baskets running meaningfully hotter than the universe median
    -- exactly what the deck's disclosed "volatility-aware ranking...
    tilting toward steadier trends" is meant to filter out. Lower
    volatility is better; the inversion happens in build_composite_scores,
    not here (this function just reports the raw number).
    """
    results = {}
    for sym, group in cm_history.groupby("symbol"):
        g = group.sort_values("trade_date")
        if len(g) < 3:
            results[sym] = np.nan
            continue
        pct_changes = g["close_price"].pct_change().dropna() * 100
        results[sym] = pct_changes.std() if not pct_changes.empty else np.nan
    return pd.Series(results).reindex(symbols)


def _percentile_rank(series: pd.Series) -> pd.Series:
    """Rank a signal to 0-100 percentile, NaNs kept as NaN (excluded, not zeroed)."""
    return series.rank(pct=True, na_option="keep") * 100


def build_composite_scores(
    rollover_pct: pd.Series,
    cost_of_carry: pd.Series,
    price_momentum: pd.Series,
    volume_trend: pd.Series,
    volatility: pd.Series = None,
) -> pd.DataFrame:
    """
    Combine the signals into a single composite score using percentile
    ranking (so signals on different scales are comparable) and the
    weights in config.SIGNAL_WEIGHTS.

    volatility is optional (backward compatible with older callers/tests
    that don't pass it) -- when given, it's inverted before ranking so
    LOWER realised volatility scores HIGHER, matching the deck's
    disclosed "volatility-aware ranking" risk control.
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
    if volatility is not None:
        frame["volatility"] = volatility

    composite = (
        _percentile_rank(frame["rollover_pct"]) * weights["rollover_pct"]
        + _percentile_rank(frame["cost_of_carry"]) * weights["cost_of_carry"]
        + _percentile_rank(frame["price_momentum"]) * weights["price_momentum"]
        + _percentile_rank(frame["volume_trend"]) * weights["volume_trend"]
    )

    if volatility is not None and "low_volatility" in weights:
        # Negate before ranking: lower volatility -> higher percentile.
        composite = composite + _percentile_rank(-frame["volatility"]) * weights["low_volatility"]

    frame["composite_score"] = composite
    return frame


def rank_universe(
    rollover_pct: pd.Series,
    cost_of_carry: pd.Series,
    price_momentum: pd.Series,
    volume_trend: pd.Series,
    volatility: pd.Series = None,
    sector_map: dict = None,
    top_n: int = None,
) -> pd.DataFrame:
    """
    Return the top_n symbols by composite score, sorted descending,
    subject to the deck's disclosed 30% single-sector cap.

    sector_map is optional (dict of symbol -> sector). When given, the
    ranking greedily walks the composite-score-sorted list and skips
    any symbol that would push its sector's count above
    config.MAX_SECTOR_WEIGHT_PCT of top_n -- e.g. 3 out of 10 stocks at
    the deck's 30% cap. Symbols missing from sector_map are treated as
    their own single-symbol "Unclassified" bucket, so they're still
    capped, just not grouped with anything else. If sector_map is None
    (e.g. the sector file couldn't be loaded), no cap is applied --
    degrades to the old unconstrained ranking rather than failing.
    """
    top_n = top_n or config.PORTFOLIO_SIZE
    scored = build_composite_scores(
        rollover_pct, cost_of_carry, price_momentum, volume_trend, volatility
    )
    scored = scored.dropna(subset=["composite_score"])
    ranked_all = scored.sort_values("composite_score", ascending=False)

    if sector_map:
        max_per_sector = max(1, int(top_n * config.MAX_SECTOR_WEIGHT_PCT / 100))
        selected = []
        sector_counts = {}
        for symbol in ranked_all.index:
            sector = sector_map.get(symbol, f"Unclassified:{symbol}")
            if sector_counts.get(sector, 0) >= max_per_sector:
                continue
            selected.append(symbol)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if len(selected) >= top_n:
                break
        ranked = ranked_all.loc[selected]
    else:
        ranked = ranked_all.head(top_n)

    result = ranked.reset_index().rename(columns={"index": "symbol"})
    result.insert(0, "rank", range(1, len(result) + 1))
    result["weight_pct"] = min(
        config.MAX_SINGLE_STOCK_WEIGHT_PCT, 100 / top_n if top_n else 0
    )
    return result
