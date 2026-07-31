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
