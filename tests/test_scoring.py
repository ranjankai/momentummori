"""
Unit tests for scoring.py using synthetic data shaped like NSE's
documented bhavcopy schema. These do NOT touch the network — they
verify the math and ranking logic in isolation.
"""

import pandas as pd
import pytest

import scoring


def test_normalize_cm_columns_renames_and_parses_date():
    raw = pd.DataFrame(
        {
            "TckrSymb": ["RELIANCE"],
            "ClsPric": [2500.0],
            "TtlTradgVol": [1_000_000],
            "TradDt": ["2026-07-27"],
        }
    )
    norm = scoring.normalize_cm_columns(raw)
    assert list(norm.columns) == ["symbol", "close_price", "volume", "trade_date"]
    assert norm["trade_date"].iloc[0] == pd.Timestamp("2026-07-27")


def test_normalize_cm_columns_keeps_only_eq_series_and_dedupes():
    """
    Regression test: NSE lists some symbols under more than one series
    on the same day (e.g. IIFL under both "EQ" and "BL"). Left in, this
    produced two rows per symbol per date, which crashed
    compute_cost_of_carry live with "truth value of a Series is
    ambiguous" because spot_prices.get(symbol) returned more than one
    row instead of a scalar.
    """
    raw = pd.DataFrame(
        {
            "TckrSymb": ["IIFL", "IIFL", "RELIANCE"],
            "SctySrs": ["BL", "EQ", "EQ"],
            "ClsPric": [590.00, 607.85, 2500.0],
            "TtlTradgVol": [1000, 50000, 1_000_000],
            "TradDt": ["2026-07-30", "2026-07-30", "2026-07-30"],
        }
    )
    norm = scoring.normalize_cm_columns(raw)
    iifl_rows = norm[norm["symbol"] == "IIFL"]
    assert len(iifl_rows) == 1
    assert iifl_rows.iloc[0]["close_price"] == 607.85  # the EQ row, not BL
    assert not norm.duplicated(subset=["symbol", "trade_date"]).any()


def test_normalize_cm_columns_keeps_symbol_with_no_eq_series():
    """A symbol with only a non-EQ series shouldn't be silently dropped."""
    raw = pd.DataFrame(
        {
            "TckrSymb": ["ODDONE"],
            "SctySrs": ["BE"],
            "ClsPric": [42.0],
            "TtlTradgVol": [100],
            "TradDt": ["2026-07-30"],
        }
    )
    norm = scoring.normalize_cm_columns(raw)
    assert len(norm[norm["symbol"] == "ODDONE"]) == 1


def test_compute_price_momentum_matches_manual_calc():
    history = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
            "trade_date": pd.to_datetime(
                ["2026-06-01", "2026-06-15", "2026-07-01"] * 2
            ),
            "close_price": [100, 110, 120, 200, 190, 180],
        }
    )
    result = scoring.compute_price_momentum(history, ["AAA", "BBB", "CCC"])
    assert result["AAA"] == pytest.approx((120 - 100) / 100 * 100)
    assert result["BBB"] == pytest.approx((180 - 200) / 200 * 100)
    assert pd.isna(result["CCC"])  # no data at all for CCC


def test_compute_volume_trend_rising_vs_falling():
    history = pd.DataFrame(
        {
            "symbol": ["RISING"] * 4 + ["FALLING"] * 4,
            "trade_date": pd.to_datetime(
                ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"] * 2
            ),
            "volume": [100, 100, 300, 300] + [300, 300, 100, 100],
        }
    )
    result = scoring.compute_volume_trend(history, ["RISING", "FALLING"])
    assert result["RISING"] > 1
    assert result["FALLING"] < 1


def test_compute_rollover_pct_near_next_month():
    fo = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB", "BBB"],
            "instrument_type": ["FUTSTK"] * 4,
            "expiry_date": pd.to_datetime(
                ["2026-07-30", "2026-08-27", "2026-07-30", "2026-08-27"]
            ),
            "open_interest": [1000, 4000, 4000, 1000],  # AAA: heavy rollover, BBB: light
        }
    )
    result = scoring.compute_rollover_pct(fo, "2026-07-27", ["AAA", "BBB"])
    assert result["AAA"] == pytest.approx(4000 / 5000 * 100)
    assert result["BBB"] == pytest.approx(1000 / 5000 * 100)
    assert result["AAA"] > result["BBB"]


def test_compute_cost_of_carry_premium_vs_discount():
    fo = pd.DataFrame(
        {
            "symbol": ["PREMIUM", "DISCOUNT"],
            "instrument_type": ["FUTSTK", "FUTSTK"],
            "expiry_date": pd.to_datetime(["2026-08-27", "2026-08-27"]),
            "open_interest": [1000, 1000],
            "settlement_price": [105.0, 95.0],
            "trade_date": pd.to_datetime(["2026-07-27", "2026-07-27"]),
        }
    )
    spot_prices = pd.Series({"PREMIUM": 100.0, "DISCOUNT": 100.0})
    result = scoring.compute_cost_of_carry(fo, spot_prices, ["PREMIUM", "DISCOUNT"])
    assert result["PREMIUM"] > 0
    assert result["DISCOUNT"] < 0


def test_compute_cost_of_carry_skips_contract_expiring_today():
    """
    Regression test: on a monthly F&O expiry day, the nearest-listed
    contract expires that same day for every stock at once (0 days to
    expiry). Annualising a 0-day cost of carry is meaningless, and
    because it happens across the whole universe simultaneously, an
    earlier version of this function returned NaN for every symbol on
    expiry days -- which zeroed out the composite score and produced
    zero ranked stocks for that day. Caught live on a backtest run that
    landed on 31 Jul 2025, a real NSE monthly expiry. The fix: skip
    contracts expiring on/before their own trade date and use the next
    one out.
    """
    fo = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB", "BBB"],
            "instrument_type": ["FUTSTK", "FUTSTK", "FUTSTK", "FUTSTK"],
            "expiry_date": pd.to_datetime(
                ["2026-07-31", "2026-08-28", "2026-07-31", "2026-08-28"]
            ),
            "open_interest": [1000, 4000, 2000, 3000],
            "settlement_price": [100.0, 106.0, 200.0, 194.0],
            "trade_date": pd.to_datetime(["2026-07-31"] * 4),  # expiry day itself
        }
    )
    spot_prices = pd.Series({"AAA": 99.0, "BBB": 200.0})
    result = scoring.compute_cost_of_carry(fo, spot_prices, ["AAA", "BBB"])
    # Must NOT be NaN -- should have skipped the expiring-today contract
    # and used the August one instead.
    assert result.notna().all(), f"expected both symbols priced off the next contract, got {result.to_dict()}"
    assert result["AAA"] == pytest.approx((106.0 - 99.0) / 99.0 * (365 / 28) * 100)


def test_rank_universe_orders_by_composite_and_respects_top_n():
    symbols = ["A", "B", "C", "D", "E"]
    rollover_pct = pd.Series([90, 10, 50, 70, 30], index=symbols)
    cost_of_carry = pd.Series([5, 1, 3, 4, 2], index=symbols)
    price_momentum = pd.Series([20, 1, 10, 15, 5], index=symbols)
    volume_trend = pd.Series([1.5, 0.9, 1.1, 1.3, 1.0], index=symbols)

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend, top_n=3
    )
    assert len(ranked) == 3
    assert list(ranked["rank"]) == [1, 2, 3]
    # A should be #1 given it leads on every signal
    assert ranked.iloc[0]["symbol"] == "A"
    # composite scores must be strictly descending
    scores = ranked["composite_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_rank_universe_drops_symbols_with_no_signals():
    symbols = ["A", "B"]
    rollover_pct = pd.Series([90, None], index=symbols)
    cost_of_carry = pd.Series([5, None], index=symbols)
    price_momentum = pd.Series([20, None], index=symbols)
    volume_trend = pd.Series([1.5, None], index=symbols)

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend, top_n=5
    )
    assert len(ranked) == 1
    assert ranked.iloc[0]["symbol"] == "A"


def test_compute_volatility_ranks_steadier_stock_lower():
    history = pd.DataFrame(
        {
            "symbol": ["STEADY"] * 5 + ["CHOPPY"] * 5,
            "trade_date": pd.to_datetime(["2026-07-0%d" % d for d in range(1, 6)] * 2),
            "close_price": [100, 101, 100, 101, 100] + [100, 120, 90, 130, 85],
        }
    )
    vol = scoring.compute_volatility(history, ["STEADY", "CHOPPY"])
    assert vol["STEADY"] < vol["CHOPPY"]


def test_rank_universe_prefers_lower_volatility_when_other_signals_tied():
    symbols = ["STEADY", "CHOPPY"]
    rollover_pct = pd.Series([50, 50], index=symbols)
    cost_of_carry = pd.Series([5, 5], index=symbols)
    price_momentum = pd.Series([10, 10], index=symbols)
    volume_trend = pd.Series([1.2, 1.2], index=symbols)
    volatility = pd.Series([1.0, 5.0], index=symbols)  # STEADY much calmer

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend,
        volatility=volatility, top_n=2,
    )
    assert ranked.iloc[0]["symbol"] == "STEADY"


def test_rank_universe_enforces_sector_cap():
    """
    Regression test for the concrete gap found via a real backtest: 5 of
    13 real months breached the deck's disclosed 30% sector cap (one at
    50% Metals/Mining, coinciding with the worst drawdown month). With a
    sector_map and top_n=10, no more than 3 stocks (30%) from one sector
    should be selected, even if that sector dominates the composite
    ranking.
    """
    # 6 "BANK" symbols all score higher than 4 "OTHER" symbols.
    symbols = ["BANK1", "BANK2", "BANK3", "BANK4", "BANK5", "BANK6",
               "OTHER1", "OTHER2", "OTHER3", "OTHER4"]
    rollover_pct = pd.Series([90, 89, 88, 87, 86, 85, 50, 49, 48, 47], index=symbols)
    cost_of_carry = pd.Series([9, 8, 7, 6, 5, 4, 3, 2, 1, 0.5], index=symbols)
    price_momentum = pd.Series([20, 19, 18, 17, 16, 15, 10, 9, 8, 7], index=symbols)
    volume_trend = pd.Series([1.5, 1.4, 1.3, 1.2, 1.1, 1.05, 1.0, 0.95, 0.9, 0.85], index=symbols)
    sector_map = {f"BANK{i}": "Bank" for i in range(1, 7)}
    sector_map.update({f"OTHER{i}": f"Sector{i}" for i in range(1, 5)})

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend,
        sector_map=sector_map, top_n=10,
    )
    bank_count = (ranked["symbol"].str.startswith("BANK")).sum()
    assert bank_count == 3, f"expected exactly 3 (30% of 10) Bank names, got {bank_count}"
    # Only 3 Bank + 4 distinct-sector Other symbols can ever qualify here
    # (3 more Banks exist but are capped out) -- top_n=10 can't be filled
    # from just 10 total candidates once 3 of the 6 Banks are excluded.
    assert len(ranked) == 7


def test_rank_universe_without_sector_map_is_unconstrained():
    """Backward compatibility: sector_map=None (or {}) behaves like before this feature existed."""
    symbols = ["BANK1", "BANK2", "BANK3", "BANK4"]
    rollover_pct = pd.Series([90, 89, 88, 87], index=symbols)
    cost_of_carry = pd.Series([9, 8, 7, 6], index=symbols)
    price_momentum = pd.Series([20, 19, 18, 17], index=symbols)
    volume_trend = pd.Series([1.5, 1.4, 1.3, 1.2], index=symbols)

    ranked = scoring.rank_universe(
        rollover_pct, cost_of_carry, price_momentum, volume_trend, top_n=4
    )
    assert len(ranked) == 4  # all 4 "Bank" names allowed through, no cap applied
