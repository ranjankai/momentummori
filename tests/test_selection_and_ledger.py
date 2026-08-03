"""
Tests on the two paths that carry money: which stocks get bought, and
what the ledger reports back.

Hermetic -- synthetic signals and a temporary ledger file, no network.

These exist because the bugs found on 03-Aug-2026 were all in mundane
assumptions about data, not in clever logic. Basket selection and the
performance figures shown to investors had no tests at all.
"""
import json
import os

import pandas as pd
import pytest

import config
import ledger
import strategy


# ---------------------------------------------------------------------------
# basket selection
# ---------------------------------------------------------------------------

def signals(rows):
    """
    rows: {symbol: (volatility, rollover, cost_of_carry, turnover_cr)}

    The index MUST be named "symbol": `rank_universe` calls reset_index()
    and then reads a "symbol" column, so an unnamed index yields "index"
    and raises a KeyError further down. compute_signals happens to set it.
    """
    df = pd.DataFrame(
        [{"volatility": v, "rollover": r, "cost_of_carry": c,
          "turnover_cr": t, "close": 100.0}
         for v, r, c, t in rows.values()],
        index=list(rows),
    )
    df.index.name = "symbol"
    return df


def linear_universe(n=30, turnover=50.0):
    """Descending volatility so the expected ranking is unambiguous."""
    return signals({f"S{i:02d}": (float(n - i), 90.0, 0.5, turnover)
                    for i in range(n)})


def test_volatility_is_scored_HIGHER_is_better():
    """
    V4 deliberately inverts the source deck: high realised volatility
    scores higher. If this ever flips, the whole strategy changes
    character and every backtest number becomes meaningless.
    """
    basket, _ = strategy.rank_universe(linear_universe(), sector_map=None)
    assert basket["symbol"].iloc[0] == "S00", "highest volatility must rank first"
    assert basket["symbol"].iloc[-1] != "S00"


def test_weights_are_the_documented_ones():
    assert config.V4_WEIGHTS == {"volatility": 0.50, "rollover": 0.30,
                                 "cost_of_carry": 0.20}
    assert abs(sum(config.V4_WEIGHTS.values()) - 1.0) < 1e-9


def test_sector_cap_is_enforced():
    """No more than 3 of 10 from one sector (MAX_SECTOR_WEIGHT_PCT = 30)."""
    sig = linear_universe(30)
    sectors = {f"S{i:02d}": ("Banks" if i < 8 else f"Sector{i}")
               for i in range(30)}
    basket, _ = strategy.rank_universe(sig, sector_map=sectors)
    picked_banks = [s for s in basket["symbol"] if sectors[s] == "Banks"]
    assert len(picked_banks) == 3, f"sector cap breached: {picked_banks}"
    assert len(basket) == config.PORTFOLIO_SIZE


def test_liquidity_floor_drops_thin_names():
    """A thin name must not enter however well it scores."""
    sig = linear_universe(30)
    sig.loc["S00", "turnover_cr"] = config.MIN_TURNOVER_CRORE - 0.1
    basket, _ = strategy.rank_universe(sig, sector_map=None)
    assert "S00" not in set(basket["symbol"]), "thin name entered the basket"


def test_missing_turnover_is_not_treated_as_thin():
    """NaN turnover means unknown, not zero -- dropping it would be silent."""
    sig = linear_universe(30)
    sig.loc["S00", "turnover_cr"] = float("nan")
    basket, _ = strategy.rank_universe(sig, sector_map=None)
    assert "S00" in set(basket["symbol"])


def test_stop_and_target_are_computed_off_the_basket_close():
    sig = linear_universe(30)
    basket, _ = strategy.rank_universe(sig, sector_map=None)
    row = basket.iloc[0]
    assert row["stop_loss"] == pytest.approx(
        100.0 * (1 - config.V4_STOP_LOSS_PCT / 100), rel=1e-6)
    assert row["target"] == pytest.approx(
        100.0 * (1 + config.V4_TARGET_PCT / 100), rel=1e-6)


def test_too_few_scoreable_symbols_raises_rather_than_short_fills():
    """Better to fail loudly than to send investors a 4-stock basket."""
    with pytest.raises(strategy.StrategyError):
        strategy.rank_universe(linear_universe(4), sector_map=None)


def test_rows_with_missing_signals_are_dropped_not_zero_filled():
    sig = linear_universe(30)
    sig.loc["S00", "rollover"] = float("nan")
    basket, _ = strategy.rank_universe(sig, sector_map=None)
    assert "S00" not in set(basket["symbol"])


# ---------------------------------------------------------------------------
# regime stop
# ---------------------------------------------------------------------------

def stop_at_breadth(monkeypatch, value):
    monkeypatch.setattr(strategy, "market_breadth",
                        lambda as_of, symbols, hist: value)
    return strategy.resolve_stop_pct("2026-02-24", symbols=["X"],
                                     price_hist={"x": 1})


def test_regime_stop_thresholds(monkeypatch):
    """
    Breadth at or above the threshold is the TIGHT stop. The boundary
    matters: Feb-2026 read 48.1%, three points from flipping.
    """
    t = config.REGIME_BREADTH_THRESHOLD
    assert stop_at_breadth(monkeypatch, t) == config.REGIME_STOP_TIGHT_PCT
    assert stop_at_breadth(monkeypatch, t + 0.1) == config.REGIME_STOP_TIGHT_PCT
    assert stop_at_breadth(monkeypatch, t - 0.1) == config.REGIME_STOP_WIDE_PCT


def test_unknown_breadth_falls_back_to_the_fixed_stop(monkeypatch):
    """
    A nan must not silently pick a stop. This is exactly what bit
    tools_cycle_compare: it loaded 90 days of history, market_breadth
    returned nan, and the run quietly used the 5% stop when the correct
    regime answer was 10%.
    """
    assert stop_at_breadth(monkeypatch, float("nan")) == config.V4_STOP_LOSS_PCT


def test_no_price_history_falls_back_rather_than_raising():
    """A position must never end up without a stop."""
    assert strategy.resolve_stop_pct("2026-02-24") == config.V4_STOP_LOSS_PCT


# ---------------------------------------------------------------------------
# surveillance veto -- the basket you actually own
# ---------------------------------------------------------------------------

def stub_veto(monkeypatch, vetoed):
    import surveillance
    monkeypatch.setattr(surveillance, "fetch_vetoed_symbols",
                        lambda session=None: dict(vetoed))
    return surveillance


def test_veto_drops_and_backfills_to_full_size(monkeypatch):
    surveillance = stub_veto(monkeypatch, {"S02": "ASM Stage I"})
    sig = linear_universe(30)
    basket, full = strategy.rank_universe(sig, sector_map=None)
    kept, dropped, added, ran = surveillance.apply_veto(
        basket, list(full.index), None)
    assert ran is True
    assert "S02" not in kept
    assert [d[0] for d in dropped] == ["S02"]
    assert len(kept) == config.PORTFOLIO_SIZE, "veto left the basket short"
    assert added, "no backfill happened"


def test_veto_backfill_respects_the_sector_cap(monkeypatch):
    surveillance = stub_veto(monkeypatch, {"S00": "ASM Stage I"})
    sig = linear_universe(30)
    sectors = {f"S{i:02d}": ("Banks" if i < 8 else f"Sector{i}")
               for i in range(30)}
    kept, _dropped, _added, _ran = surveillance.apply_veto(
        strategy.rank_universe(sig, sector_map=sectors)[0],
        list(strategy.rank_universe(sig, sector_map=sectors)[1].index),
        sectors)
    banks = [s for s in kept if sectors[s] == "Banks"]
    assert len(banks) <= 3, f"backfill breached the sector cap: {banks}"


def test_a_vetoed_name_removed_from_the_ranking_is_never_bought():
    """
    The bug found 03-Aug-2026: simulate_month fills slots by walking
    `ranked_order`, NOT `basket_symbols`. Passing the vetoed list only as
    basket_symbols left the walk buying the vetoed name anyway, so the
    evening note tracked KALYANKJIL (ASM Stage I) while the real book
    held ADANIGREEN. The veto must come out of the RANKING.
    """
    import datetime as dt

    def bars(sym, n=4, price=100.0):
        out, d = {}, dt.date(2026, 1, 1)
        for _ in range(n):
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            out.setdefault(d, {})[sym] = price
            d += dt.timedelta(days=1)
        return out

    days, frames = [], {}
    d = dt.date(2026, 1, 1)
    for _ in range(4):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        frames[d] = pd.DataFrame(
            [{"open_price": 100.0, "high_price": 101.0,
              "low_price": 99.0, "close_price": 100.0} for _ in range(3)],
            index=["GOOD1", "VETOED", "GOOD2"])
        days.append(d)
        d += dt.timedelta(days=1)

    ranked_with = ["GOOD1", "VETOED", "GOOD2"]
    ranked_without = ["GOOD1", "GOOD2"]
    sectors = {"GOOD1": "A", "VETOED": "B", "GOOD2": "C"}

    res = strategy.simulate_month(ranked_with, frames, days, sectors,
                                  top_n=2, carry_forward=False)
    assert "VETOED" in {p.symbol for p in res.open_positions}, \
        "fixture wrong: the vetoed name should be bought when ranked"

    res2 = strategy.simulate_month(ranked_without, frames, days, sectors,
                                   top_n=2, carry_forward=False)
    assert "VETOED" not in {p.symbol for p in res2.open_positions}


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_FILE", str(path), raising=False)
    monkeypatch.setattr(config, "LEDGER_FILE", str(path), raising=False)
    return path


def write_ledger(path, records):
    path.write_text(json.dumps(records))


def test_performance_sum_and_compounding_differ_correctly(temp_ledger,
                                                          monkeypatch):
    """
    +10% then -10% is 0% added but -1% compounded. Investors are quoted
    the additive figure; the compounded one is what capital did. Mixing
    them up misstates the track record.
    """
    monkeypatch.setattr(ledger, "monthly_summary",
                        lambda: [{"month": "2026-01", "return_pct": 10.0},
                                 {"month": "2026-02", "return_pct": -10.0}])
    perf = ledger.performance()
    assert perf["absolute_sum"] == pytest.approx(0.0)
    assert perf["absolute_comp"] == pytest.approx(-1.0)
    assert perf["n_months"] == 2


def test_performance_flags_short_history_as_extrapolated(monkeypatch):
    """A CAGR from 2 months is an extrapolation and must say so."""
    monkeypatch.setattr(ledger, "monthly_summary",
                        lambda: [{"month": "2026-01", "return_pct": 5.0},
                                 {"month": "2026-02", "return_pct": 5.0}])
    assert ledger.performance()["extrapolated"] is True


def test_performance_on_an_empty_ledger_is_zero_not_a_crash(monkeypatch):
    monkeypatch.setattr(ledger, "monthly_summary", lambda: [])
    perf = ledger.performance()
    assert perf["absolute_sum"] == 0.0
    assert perf["n_months"] == 0
    assert perf["cagr"] is None


def test_closed_trades_deduplicates_on_symbol_and_date(monkeypatch):
    """
    The daily run records the same exit every evening until the cycle
    rolls. Counting it twice would inflate the trade count and the win
    rate shown to investors.
    """
    same = {"symbol": "ACME", "exit_date": "2026-02-10", "pnl_pct": -5.0}
    monkeypatch.setattr(ledger, "history", lambda kind=None: [
        {"exits": [same]}, {"exits": [dict(same)]},
        {"exits": [{"symbol": "ACME", "exit_date": "2026-03-11",
                    "pnl_pct": 3.0}]},
    ])
    trades = ledger.closed_trades()
    assert len(trades) == 2
    assert [t["exit_date"] for t in trades] == ["2026-02-10", "2026-03-11"]
