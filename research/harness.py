"""
The ONLY sanctioned way to backtest in research/.

Every function here delegates the position walk to
`strategy.simulate_month`. Nothing in this file re-implements stops,
targets, fills or corporate-action handling -- that is what produced the
`sim()` helper's bogus levels on 02-Aug-2026 and the seven forked scripts
deleted on 03-Aug-2026.

If you need behaviour `simulate_month` does not have, add a parameter to
`simulate_month`. Do not fork the loop.

CLASSIFIER
  Backtests run with the corporate-action classifier ON, the same as the
  live report, so both handle a 5:4-style bonus the price band alone
  would miss. Accepted trade-off: a grey-zone move fires an NSE fetch and
  a Gemini call, so a cycle takes ~24s instead of being instant, and a
  re-run can in principle differ if the model answers differently.
  `simulate_month(use_classifier=False)` turns it off if a reproducible
  run is ever needed.

CONVENTION
  Fresh-start and additive. Rs100 is deployed on the first session after
  an expiry and fully closed at the first session after the next expiry.
  Monthly returns are SUMMED, never compounded, because investors enter
  and exit at will and each month's number must belong to whoever was
  invested that month. `carry_forward=False` throughout.

    from research.harness import cycle_dates, run_cycle
    ex, nx, hold = cycle_dates(2026, 3)
    res = run_cycle(picks, ex, nx)
    print(res.return_pct)
"""
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)
import nse_client                                           # noqa: E402
import scoring                                              # noqa: E402
import strategy                                             # noqa: E402

_UNI = None
_SEC = None


def universe():
    global _UNI
    if _UNI is None:
        _UNI = strategy.load_fo_universe()
    return _UNI


def sectors():
    global _SEC
    if _SEC is None:
        _SEC = strategy.load_sector_map()
    return _SEC


def cycle_dates(year, month):
    """(entry expiry, next expiry) for the cycle STARTING after `month`."""
    td = strategy.known_trading_days()
    ex = strategy.expiry_for(year, month, trading_days=td)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return ex, strategy.expiry_for(ny, nm, trading_days=td)


def prices(ex, nx):
    """
    Frames covering the lookback AND the holding window, plus the first
    session after `nx` so the cycle can be closed at the rollover open.
    """
    uni = universe()
    merged = dict(strategy.load_price_history(ex, uni))
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12),
                                              uni, days=20))
    return merged


def v4_basket(ex, top_n=10):
    """
    The live engine's basket at `ex`, plus the regime stop it would use.

    DELIBERATELY calls rank_universe, NOT strategy.basket_for, so the
    surveillance veto is NOT applied. NSE publishes ASM as a current
    snapshot and does not archive it per date, so vetoing a historical
    basket with today's list is look-ahead. The consequence is a real gap:
    the LIVE basket has the veto and the BACKTEST does not, so they can
    differ by a name. On the 28-Jul-2026 expiry the live basket dropped
    KALYANKJIL (ASM Stage I) and backfilled ADANIGREEN; a backtest of that
    cycle would hold KALYANKJIL. Quantifying that gap needs an archived
    ASM history we do not have.
    """
    uni, sec = universe(), sectors()
    hist = strategy.load_price_history(ex, uni)
    stop_pct = strategy.resolve_stop_pct(ex, uni, hist)
    breadth = strategy.market_breadth(ex, uni, hist)
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, full = strategy.rank_universe(sig, sec)
    return (basket["symbol"].tolist()[:top_n], list(full.index),
            stop_pct, breadth)


def run_cycle(picks, ex, nx, stop_pct=None, target_pct=None,
              price_by_date=None, ranked_order=None, top_n=10,
              ratchet_trigger_pct=None, ratchet_lock_pct=None):
    """
    One fresh-start cycle through the production engine.

    `picks` seeds the slots; `ranked_order` (defaults to `picks`) is what
    simulate_month walks for replacements, which are off in production
    anyway (V4_REDEPLOY_ENABLED = False).

    `ratchet_trigger_pct`/`ratchet_lock_pct` pass straight through to
    `strategy.simulate_month` -- see its docstring. Both None (default)
    reproduces the live engine exactly.
    """
    merged = price_by_date if price_by_date is not None else prices(ex, nx)
    after = [d for d in sorted(merged) if d > nx]
    roll = after[0] if after else nx
    hold = [d for d in sorted(merged) if ex < d <= roll]
    return strategy.simulate_month(
        ranked_order or list(picks), merged, hold, sectors(),
        basket_symbols=list(picks), top_n=top_n,
        stop_pct=stop_pct, target_pct=target_pct,
        ratchet_trigger_pct=ratchet_trigger_pct,
        ratchet_lock_pct=ratchet_lock_pct,
        carry_forward=False)          # fresh start, always


def walk_forward(months, top_n=10, stop_pct=None, basket_fn=None):
    """
    Run a list of (year, month) cycles and return per-cycle results.

    `basket_fn(ex)` supplies the picks; defaults to the live V4 basket.
    Returns [{month, breadth, stop, ret, trades, picks}], and the additive
    sum is simply sum(r["ret"]).
    """
    out = []
    for y, m in months:
        ex, nx = cycle_dates(y, m)
        picks, ranked, regime_stop, breadth = v4_basket(ex, top_n)
        if basket_fn is not None:
            picks = basket_fn(ex)
            ranked = picks
        res = run_cycle(picks, ex, nx, stop_pct=stop_pct or regime_stop,
                        ranked_order=ranked, top_n=top_n)
        out.append(dict(month=f"{y}-{m:02d}", breadth=breadth,
                        stop=stop_pct or regime_stop, ret=res.return_pct,
                        trades=res.trades, picks=picks))
    return out
