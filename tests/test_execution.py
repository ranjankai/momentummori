"""
Regression tests for the three defects found on 03-Aug-2026.

All synthetic, no network. Each test fails against the code as it stood
that morning:

  1. a split mid-holding booked a ~-60% loss and fired a false EXIT
  2. a session that opened below the stop still booked the stop price
  3. the daily report raised on any date before the current month's expiry

The point of writing them is that self-verification only catches cases
you already have in mind. These encode the ones that were missed.
"""
import datetime as dt

import pandas as pd
import pytest

import daily_report
import strategy


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """
    Hermetic by default. `adjust_holding_window` consults the corporate
    action classifier, which hits NSE and Gemini; left live these tests
    took 32s and their result depended on what a model said about a
    made-up symbol. Tests that WANT the classifier stub it explicitly.
    """
    monkeypatch.setattr(strategy.config, "CORP_ACTION_LLM_ENABLED", False,
                        raising=False)
    monkeypatch.setattr(strategy.config, "CORP_ACTION_GREY_ZONE_ENABLED", False,
                        raising=False)


def stub_classifier(monkeypatch, classification, ratio):
    """Force one verdict out of the classifier without any network."""
    import corporate_actions
    monkeypatch.setattr(
        corporate_actions, "classify",
        lambda symbol, day, prev_close, close, session=None: {
            "symbol": symbol, "classification": classification,
            "adjustment_ratio": ratio, "reconciles": True},
    )
    monkeypatch.setattr(strategy.config, "CORP_ACTION_LLM_ENABLED", True,
                        raising=False)
    monkeypatch.setattr(strategy.config, "CORP_ACTION_GREY_ZONE_ENABLED", True,
                        raising=False)


def frame(rows):
    """rows: {symbol: (open, high, low, close)} -> a bhavcopy-shaped frame."""
    return pd.DataFrame(
        [{"open_price": o, "high_price": h, "low_price": l, "close_price": c}
         for o, h, l, c in rows.values()],
        index=list(rows),
    )


def series(sym, bars, start=dt.date(2026, 1, 1)):
    """bars: list of (open, high, low, close) -> {date: frame}, weekdays only."""
    out, d = {}, start
    for bar in bars:
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        out[d] = frame({sym: bar})
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 1. corporate actions over the holding window
# ---------------------------------------------------------------------------

def test_split_is_not_a_loss():
    """
    A 1:2 split halves the price overnight. Nobody lost anything, so it
    must not read as a -50% move. Before the fix this tripped the stop.
    """
    px = series("ACME", [
        (100, 101, 99, 100),
        (100, 102, 99, 101),
        (50.5, 51, 50, 50.5),      # 1:2 split, price halves
        (50.5, 51, 50, 50.8),
    ])
    dates = sorted(px)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"])

    closes = [float(adj[d].at["ACME", "close_price"]) for d in dates]
    ratios = [closes[i + 1] / closes[i] for i in range(len(closes) - 1)]
    assert min(ratios) > 0.9, f"split still reads as a crash: {ratios}"


def test_clean_series_is_returned_untouched():
    """The no-action path must not copy frames -- it runs on every cycle."""
    px = series("ACME", [(100, 101, 99, 100), (100, 102, 99, 101),
                         (101, 103, 100, 102)])
    dates = sorted(px)
    assert strategy.adjust_holding_window(px, dates, symbols=["ACME"]) is px


def test_genuine_fall_inside_the_band_survives():
    """
    A -15% day is a real move and must NOT be erased. F&O names carry a
    +/-10% dynamic price band that can flex, so falls of this size happen.
    """
    px = series("ACME", [(100, 101, 99, 100), (100, 101, 84, 85),
                         (85, 86, 84, 85)])
    dates = sorted(px)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"])
    assert float(adj[dates[1]].at["ACME", "close_price"]) == pytest.approx(85)


def test_without_the_classifier_the_band_alone_still_protects():
    """A -35% day is outside the hard band, so it is adjusted regardless."""
    px = series("ACME", [(100, 101, 99, 100), (100, 101, 64, 65),
                         (65, 66, 64, 65)])
    dates = sorted(px)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"],
                                         use_classifier=False)
    assert float(adj[dates[1]].at["ACME", "close_price"]) != pytest.approx(65)


def test_classifier_catches_a_bonus_the_band_misses(monkeypatch):
    """
    A 5:4 bonus is a 0.80 ratio -- inside the hard band, so the band alone
    never sees it. The filings do. This is why the classifier is wired in.
    """
    px = series("ACME", [(100, 101, 99, 100), (80, 81, 79, 80),
                         (80, 81, 79, 80)])
    dates = sorted(px)
    assert strategy.adjust_holding_window(px, dates, symbols=["ACME"],
                                          use_classifier=False) is px

    stub_classifier(monkeypatch, "BONUS", 0.80)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"])
    closes = [float(adj[d].at["ACME", "close_price"]) for d in dates]
    assert closes[1] == pytest.approx(closes[0], rel=0.02), \
        f"bonus not neutralised: {closes}"


def test_a_real_move_on_the_same_day_as_a_split_is_preserved(monkeypatch):
    """
    The classifier's RATIO must be divided out, not the observed move.

    1:2 split on a day the stock also fell 10%: raw prices go 100 -> 45.
    Scaling by 1/0.45 restates it as 100 -> 100 and erases the real loss.
    Scaling by the classifier's 1/0.5 gives 100 -> 90, which is what
    actually happened to the holder.
    """
    px = series("ACME", [(100, 101, 99, 100), (45, 46, 44, 45),
                         (45, 46, 44, 45)])
    dates = sorted(px)
    stub_classifier(monkeypatch, "SPLIT", 0.5)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"])
    closes = [float(adj[d].at["ACME", "close_price"]) for d in dates]
    assert closes[1] == pytest.approx(90.0, rel=1e-3), \
        f"real -10% move erased along with the split: {closes}"


def test_band_fallback_still_divides_out_the_observed_move(monkeypatch):
    """Without a classifier verdict the band behaviour must be unchanged."""
    px = series("ACME", [(100, 101, 99, 100), (50, 51, 49, 50),
                         (50, 51, 49, 50)])
    dates = sorted(px)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"],
                                         use_classifier=False)
    closes = [float(adj[d].at["ACME", "close_price"]) for d in dates]
    assert closes[1] == pytest.approx(closes[0], rel=1e-6)


def test_classifier_keeps_a_genuine_grey_zone_fall(monkeypatch):
    """A real -20% day must survive when the filings show no action."""
    px = series("ACME", [(100, 101, 99, 100), (80, 81, 79, 80),
                         (80, 81, 79, 80)])
    dates = sorted(px)
    stub_classifier(monkeypatch, "GENUINE_MOVE", 1.0)
    assert strategy.adjust_holding_window(px, dates, symbols=["ACME"]) is px


def test_classifier_cannot_veto_the_hard_band(monkeypatch):
    """
    Safety rule. A -50% day is not available to an F&O stock under a
    +/-10% price band, so it is an action whatever the model says. A thin
    or stale filings feed returns GENUINE_MOVE, and honouring that would
    reintroduce the BSE bug: a fake -50% loss and a false EXIT alert.
    """
    px = series("ACME", [(100, 101, 99, 100), (50, 51, 49, 50),
                         (50, 51, 49, 50)])
    dates = sorted(px)
    stub_classifier(monkeypatch, "GENUINE_MOVE", 1.0)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"])
    closes = [float(adj[d].at["ACME", "close_price"]) for d in dates]
    assert closes[1] == pytest.approx(closes[0], rel=0.02), \
        f"classifier vetoed the hard band: {closes}"


def test_back_adjust_keeps_recent_prices_real():
    """
    Feature windows must not restate TODAY's price onto an old basis.
    BAJFINANCE read Rs11,352 under forward scaling when it trades at
    Rs1,141; the 52-week high is what needs restating, not the close.
    """
    px = series("ACME", [
        (1000, 1010, 990, 1000),
        (1000, 1020, 990, 1010),
        (101, 102, 100, 101),          # 1:10, price divides by ten
        (101, 103, 100, 102),
    ])
    dates = sorted(px)
    adj = strategy.adjust_holding_window(px, dates, symbols=["ACME"],
                                         back_adjust=True)
    last = float(adj[dates[-1]].at["ACME", "close_price"])
    high = max(float(adj[d].at["ACME", "high_price"]) for d in dates)
    assert last == pytest.approx(102), "today's price was restated"
    assert high == pytest.approx(103, rel=0.02), f"52w high not adjusted: {high}"


# ---------------------------------------------------------------------------
# 2. gap through the stop
# ---------------------------------------------------------------------------

def test_gap_below_stop_fills_at_the_open_not_the_stop():
    """
    TRENT closed 3343.80 on 06-07-2026 and opened 3080.00 against a
    3120.75 stop. The stock never traded at the stop, so the fill was
    -6.24%, not -5.00%. Before the fix this booked exactly -5%.
    """
    px = series("ACME", [
        (100, 101, 99, 100),           # entry day
        (100, 102, 99, 101),
        (90, 91, 88, 89),              # opens 90, far below the 95 stop
        (89, 90, 88, 89),
    ])
    dates = sorted(px)
    res = strategy.simulate_month(
        ["ACME"], px, dates, {"ACME": "Test"},
        top_n=1, stop_pct=5.0, target_pct=40.0, carry_forward=False)

    assert res.exits, "position should have exited"
    ex = res.exits[0]
    assert ex.reason == "STOP"
    assert ex.exit_px == pytest.approx(90.0), \
        f"filled at {ex.exit_px}, expected the open"
    assert res.return_pct < -5.0, "a gap must be able to lose MORE than the stop"


def test_stop_touched_intraday_fills_at_the_stop():
    """The ordinary case: the level trades, so the resting order gets it."""
    px = series("ACME", [
        (100, 101, 99, 100),
        (100, 102, 99, 101),
        (99, 100, 94, 96),             # dips to 94, through the 95 stop
        (96, 97, 95, 96),
    ])
    dates = sorted(px)
    res = strategy.simulate_month(
        ["ACME"], px, dates, {"ACME": "Test"},
        top_n=1, stop_pct=5.0, target_pct=40.0, carry_forward=False)
    assert res.exits[0].exit_px == pytest.approx(95.0)


def test_gap_above_target_fills_at_the_open():
    """Symmetric case: a gap up fills BETTER than the 40% target."""
    # the run-up is gradual so no single day trips the corporate-action
    # band; only the final open clears the 140 target
    px = series("ACME", [
        (100, 101, 99, 100),
        (100, 130, 99, 128),           # high 130, still under the target
        (142, 145, 141, 143),          # opens 142, above the 140 target
    ])
    dates = sorted(px)
    res = strategy.simulate_month(
        ["ACME"], px, dates, {"ACME": "Test"},
        top_n=1, stop_pct=5.0, target_pct=40.0, carry_forward=False)
    assert res.exits[0].reason == "TARGET"
    assert res.exits[0].exit_px == pytest.approx(142.0)


# ---------------------------------------------------------------------------
# 3. expiry resolution
# ---------------------------------------------------------------------------

def _cached_days_through(end):
    """Weekday set ending at `end` -- mimics known_trading_days()."""
    days, d = set(), end - dt.timedelta(days=400)
    while d <= end:
        if d.weekday() < 5:
            days.add(d)
        d += dt.timedelta(days=1)
    return days


def test_governing_expiry_before_this_months_expiry():
    """
    03-Aug-2026: the August expiry (25-Aug) has not happened and is not in
    the cache. Must return the July expiry, not raise.
    """
    as_of = dt.date(2026, 8, 3)
    got = daily_report.governing_expiry(as_of, _cached_days_through(as_of))
    assert got == dt.date(2026, 7, 28)


def test_governing_expiry_on_expiry_day_returns_the_previous_one():
    """On expiry day you still hold the basket bought after the LAST one."""
    as_of = dt.date(2026, 7, 28)
    got = daily_report.governing_expiry(as_of, _cached_days_through(as_of))
    assert got == dt.date(2026, 6, 30)


def test_governing_expiry_respects_the_holiday_roll_back():
    """
    31-Mar-2026 was the last Tuesday but also Mahavir Jayanti, so the
    expiry was the 30th. On 01-Apr the governing expiry is 30-Mar.
    """
    as_of = dt.date(2026, 4, 1)
    days = _cached_days_through(as_of)
    days.discard(dt.date(2026, 3, 31))          # holiday
    assert daily_report.governing_expiry(as_of, days) == dt.date(2026, 3, 30)


def test_governing_expiry_across_the_weekday_rule_change():
    """Last Thursday to Aug-2025, last Tuesday from Sep-2025."""
    as_of = dt.date(2025, 9, 1)
    got = daily_report.governing_expiry(as_of, _cached_days_through(as_of))
    assert got == dt.date(2025, 8, 28)          # last Thursday of Aug-2025


def test_governing_expiry_january_steps_back_a_year():
    as_of = dt.date(2026, 1, 2)
    got = daily_report.governing_expiry(as_of, _cached_days_through(as_of))
    assert got == dt.date(2025, 12, 30)
