"""
Evening basket note.

WHAT IT ANSWERS
---------------
1. What already exited today, without you doing anything -- resting stop
   or target orders your broker filled intra-day.
2. What you must place at TOMORROW's open -- sells for names dropped from
   the basket, buys for empty slots.
3. What you are holding, at what basis, with what stop.
4. Month-to-date return assuming perfect execution.

WHY IT RECONSTRUCTS RATHER THAN READS A FILE
--------------------------------------------
A nightly job cannot depend on hand-maintained bookkeeping, so this
module replays the month deterministically from the governing expiry's
basket plus cached bhavcopy. It shares ONE engine with the backtest --
strategy.simulate_month -- so the trading rules exist in exactly one
place and cannot drift between the two.

"PERFECT EXECUTION" MEANS
-------------------------
- Entries fill exactly at the session open.
- Resting stop/target fill exactly at their trigger price, the moment the
  day's low/high touches it.
- No slippage, no partial fills, no brokerage, no STT.
Real fills will be worse. CONTEXT.md puts the drag at roughly 0.35% per
trade, which is applied afterwards, not here.

RETURN CONVENTION
-----------------
Equal-weight additive across slots -- the same convention as
strategy.simulate_month, so this number is directly comparable to the
backtest table in CONTEXT.md rather than being a second, subtly
different definition. tests/test_daily_report.py asserts the two agree.

EOD LIMITATION
--------------
Data is daily bhavcopy. A stop that filled at 14:00 is only visible at
the NEXT evening's run. This note tells you what to do at tomorrow's
open; it is not, and cannot be, real-time.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

import config
import strategy

logger = logging.getLogger("momentum_tracker.daily_report")


# Holding/Exit now live in strategy.py -- daily_report used to carry its
# own copies plus a second simulator, which meant every trading rule was
# implemented twice and had to be changed twice. One engine now.
Holding = strategy.Position
Exit = strategy.Exit


@dataclass
class Report:
    as_of: date
    expiry: date
    entry_date: date
    holdings: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    sell_orders: list = field(default_factory=list)
    mtd_return_pct: float = 0.0
    empty_slots: int = 0
    flagged_actions: list = field(default_factory=list)
    veto_dropped: list = field(default_factory=list)
    veto_ran: bool = True
    # Exited names marked to today's close, so a bad exit is visible:
    # if `now_pct` exceeds `exit_pct`, selling cost money.
    exited_review: list = field(default_factory=list)


def governing_expiry(as_of: date, trading_days=None) -> date:
    """
    The expiry whose basket you are currently holding: the most recent
    monthly expiry strictly before `as_of`. Positions bought the session
    after that expiry are the ones live today.
    """
    y, m = as_of.year, as_of.month

    # Resolve the RAW weekday first. expiry_for() with a trading-day set
    # rolls back to the previous session, and that roll-back can only find
    # days that are already cached -- i.e. in the past. Asking it about a
    # future expiry (3-Aug looking at 25-Aug) walks back 10 days, finds
    # nothing cached, and raises. So decide which cycle we are in using the
    # raw date, which needs no calendar and cannot fail.
    raw = strategy.expiry_for(y, m)
    if raw >= as_of and (raw - as_of).days > 10:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)

    # Within 10 days of the raw date the roll-back is safe AND necessary:
    # if the raw weekday is a holiday the true expiry is earlier, e.g.
    # 31-Mar-2026 was Mahavir Jayanti so the expiry was the 30th. Resolving
    # it properly is what keeps that one day from reporting the wrong cycle.
    exp = strategy.expiry_for(y, m, trading_days=trading_days)
    if exp >= as_of:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        exp = strategy.expiry_for(y, m, trading_days=trading_days)
    return exp


# daily_report.build() was removed 01-Sep-2026: confirmed dead code, not
# called by cmd_daily (which uses cycle_state.build()/to_report() -- see
# that module's own comments for why the incremental engine exists) or
# anywhere else in the live pipeline, backtesting, or research scripts.
# It had silently diverged from cycle_state's copy of the same logic
# twice (the 17-Aug SELL ORDERS gap and the 01-Sep Exited-section
# duplicate were BOTH fixed here first, in the dead function, before the
# real bug in cycle_state.py was found and fixed separately) -- with zero
# test coverage keeping the two in sync, despite a docstring above once
# claiming a parity test existed. See CONTEXT.md's 01-Sep-2026 session
# for the full audit. cycle_state.to_report() is now the only place a
# Report gets assembled; governing_expiry() above is still live (used by
# build_entry_sheet and cycle_state.py) and was kept.


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_money(v):
    return f"{v:,.2f}"


def _ordinal_day(d) -> str:
    """17 -> '17th', 21 -> '21st', 2 -> '2nd' -- 11/12/13 are the
    exception (all 'th') that day % 10 alone gets wrong."""
    n = d.day
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _entry_portfolio_title(expiry) -> str:
    """
    "Entry portfolio for <Mon>-<Mon> '<YY> - Existing investors" (15-Aug-
    2026, explicit instruction), e.g. "Entry portfolio for Jul-Aug '26 -
    Existing investors". End month is the calendar month after expiry's
    month -- same approach and same Dec-Jan-rollover caveat as
    entry_tracking._cycle_title (that one's ALL-CAPS full month names for
    the Day-1+ header; this is title-case 3-letter abbreviations for the
    Day-0 existing-investor sheet -- deliberately different casing per
    the two separate instructions that specified each).
    """
    import calendar
    start_abbr = expiry.strftime("%b")
    end_month_num = expiry.month % 12 + 1
    end_abbr = calendar.month_abbr[end_month_num]
    yy = f"{expiry.year % 100:02d}"
    return f"Entry portfolio for {start_abbr}-{end_abbr} '{yy} - Existing investors"


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def render_performance(perf: dict) -> str:
    """
    Expiry-evening performance note. Deliberately just the numbers.

    MONTH_NAMES avoids locale surprises from strftime on Windows.
    """
    months = perf["months"]
    if not months:
        return ("<b>Portfolio performance</b>\n\nNo completed months recorded "
                "yet — the ledger starts from your first daily run.")

    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    L = ["<b>Portfolio performance</b>"]
    for r in months:
        y, m, _ = r["expiry"].split("-")
        # Label by the HOLDING month (expiry + 1), not the expiry's own
        # month -- money from a 30-Jun expiry sits in the market through
        # JULY (entry_date the next session), so it belongs on the "Jul"
        # row. Every other date label in this codebase (_cycle_title,
        # _entry_portfolio_title) already uses this convention; this one
        # didn't, and was mislabeling every row one month early
        # (25-Aug-2026 fix).
        hold_month = int(m) % 12 + 1
        hold_year = int(y) + 1 if int(m) == 12 else int(y)
        sign = "+" if r["return_pct"] >= 0 else ""
        L.append(f"{names[hold_month - 1]} {hold_year}: {sign}{r['return_pct']:.2f}%")

    # Headline is the additive sum -- the same convention as the monthly
    # rows above, so the column visibly adds up. CAGR below is still
    # derived from the COMPOUNDED figure, which is the only valid base
    # for annualising.
    s = "+" if perf["absolute_sum"] >= 0 else ""
    L.append(f"<b>Total: {s}{perf['absolute_sum']:.2f}% absolute sum</b>")

    if perf["cagr"] is not None:
        s2 = "+" if perf["cagr"] >= 0 else ""
        note = ""
        if perf["extrapolated"]:
            note = (f" ((!) extrapolated from {perf['n_months']} month(s) — "
                    f"not a track record until 1Y)")
        L.append(f"<b>CAGR: {s2}{perf['cagr']:.1f}%</b>{note}")
    return "\n".join(L).strip()


TICK_BANDS = (
    # (price ceiling for this band, tick size). NSE price-band tick
    # regime, last revised 15-Apr-2025 (applies to both cash and F&O
    # stock derivatives, same tick as the underlying CM security).
    (250, 0.01),
    (1000, 0.05),
    (5000, 0.10),
    (10000, 0.50),
    (20000, 1.00),
    (float("inf"), 5.00),
)


def _round_to_tick(price: float) -> float:
    """
    Snap `price` to the nearest valid NSE tick for its own price band
    (01-Sep-2026 addition). A limit/SL/target price that isn't a multiple
    of its band's tick literally cannot be placed at a broker -- caught
    on POWERINDIA's real SL of 31,706.25: that stock trades above
    Rs 20,000, where the tick is Rs 5, so the nearest placeable prices
    are 31,705 or 31,710, not the unrounded 5%-off-anchor figure. Below
    Rs 1,000 (most of this basket) the tick is 0.05 or smaller, where the
    gap is a few paise and was judged not worth rounding for (27-Aug-2026
    discussion) -- but that judgment doesn't hold once a name prices
    into the wider bands, where the same unrounded math produces a
    genuinely unplaceable order, not just an imprecise one.
    """
    if price <= 0:
        return price
    for ceiling, tick in TICK_BANDS:
        if price <= ceiling:
            return round(round(price / tick) * tick, 2)
    return price  # unreachable -- TICK_BANDS' last ceiling is inf


def _compute_stock_entry_band(sym: str, hist: dict, close: float) -> tuple:
    """
    Compute dynamic limit entry range (lo, hi, band_pct) for `sym` using
    recent OHLC daily range / gap volatility from `hist`.
    """
    default_b = getattr(config, "ENTRY_BAND_PCT", 2.0) / 100.0
    if not hist or close <= 0:
        return close * (1 - default_b), close * (1 + default_b), default_b * 100.0

    lookback = getattr(config, "ENTRY_BAND_LOOKBACK_DAYS", 20)
    dates = sorted(hist.keys())[-lookback:]
    ranges, gaps = [], []
    prev_close = None
    for d in dates:
        frame = hist[d]
        if frame is None or sym not in frame.index:
            continue
        row = frame.loc[sym]
        c = float(row.get("close_price", 0) or 0)
        h = float(row.get("high_price", 0) or 0)
        l = float(row.get("low_price", 0) or 0)
        o = float(row.get("open_price", 0) or 0)
        if c > 0 and h > 0 and l > 0:
            ranges.append((h - l) / c)
        if o > 0 and prev_close and prev_close > 0:
            gaps.append(abs(o - prev_close) / prev_close)
        if c > 0:
            prev_close = c

    if not ranges:
        return close * (1 - default_b), close * (1 + default_b), default_b * 100.0

    med_range = float(np.median(ranges))
    med_gap = float(np.median(gaps)) if gaps else 0.0
    raw_band = max(med_range, med_gap) * 100.0
    min_b = getattr(config, "ENTRY_BAND_MIN_PCT", 1.5)
    max_b = getattr(config, "ENTRY_BAND_MAX_PCT", 6.0)
    band_pct = max(min_b, min(max_b, raw_band))
    b_frac = band_pct / 100.0
    return close * (1 - b_frac), close * (1 + b_frac), band_pct


def _compute_min_portfolio_sizing(rows: list) -> dict:
    """
    Minimum total portfolio (capped at Rs 5,00,000) where every stock
    lands within config.ENTRY_MAX_WEIGHT_DEV_PCT (10%) of its equal-
    weight slot.

    HOW THE SLOT SIZE IS SET
    -------------------------
    Take the priciest stock in the basket. Its own band low (entry_lo)
    is the cheapest it's realistically going to be. Set the slot to
    exactly that price -- that stock buys 1 share at exactly its band's
    low edge, 0% deviation, and there is no smaller slot that could still
    fit it. Portfolio = slot x 10. No search needed: this IS the minimum,
    by construction.

    HOW EVERY OTHER STOCK IS FIT TO THAT SLOT
    -------------------------------------------
    Entry isn't a single price -- _compute_stock_entry_band already gives
    a tradeable range [entry_lo, entry_hi] sized for near-certain fill.
    So for the fixed slot and a whole-share count n, the price slot/n is
    a placeable limit order as long as it sits inside that band -- and if
    it does, that stock lands on its slot EXACTLY, not just "close enough
    via rounding". n is checked against its immediate neighbours too
    (n-1, n+1), since moving one share shifts the required price
    materially -- whichever lands closest to the band wins.
    """
    valid_rows = [r for r in rows
                  if r.get("entry_hi") and r["entry_hi"] > 0
                  and r.get("entry_lo") and r["entry_lo"] > 0
                  and r.get("close") and r["close"] > 0]
    if not valid_rows:
        return {"min_portfolio": 0, "slot_target": 0, "max_dev_pct": 0.0,
                "total_invested": 0, "shares": {}, "unsatisfied": [],
                "capped": False}

    slots = getattr(config, "PORTFOLIO_SIZE", 10)
    max_allowed_dev = getattr(config, "ENTRY_MAX_WEIGHT_DEV_PCT", 10.0)
    ceiling = 500000

    priciest = max(valid_rows, key=lambda r: r["entry_hi"])
    slot_target = priciest["entry_lo"]
    portfolio = slot_target * slots

    capped = False
    if portfolio > ceiling:
        # The priciest stock's own floor doesn't fit under Rs 5L --
        # take the cap instead and let the deviation checks below show
        # who's actually left out, rather than silently exceeding it.
        capped = True
        portfolio = ceiling
        slot_target = portfolio / slots

    details, unsatisfied, max_dev = {}, [], 0.0
    for r in valid_rows:
        n, price, dev = _solve_shares_to_slot(slot_target, r)
        details[r["symbol"]] = (n, price, dev)
        max_dev = max(max_dev, dev)
        if dev > max_allowed_dev:
            unsatisfied.append(r["symbol"])

    shares_map = {}
    total_invested = 0.0
    for sym, (n, price, dev) in details.items():
        shares_map[sym] = {"shares": n, "limit_price": price,
                           "invested": n * price, "dev_pct": dev}
        total_invested += n * price

    return {
        "min_portfolio": round(portfolio),
        "slot_target": slot_target,
        "max_dev_pct": max_dev,
        "total_invested": total_invested,
        "shares": shares_map,
        "unsatisfied": unsatisfied,
        "capped": capped,
    }


def _solve_shares_to_slot(slot: float, r: dict) -> tuple:
    """
    Best (whole-share count, limit price, deviation %) fitting one stock's
    tradeable [entry_lo, entry_hi] band to a fixed `slot` rupee size.
    Factored out of _compute_min_portfolio_sizing so _resolve_shares_to_
    target below can fit a DIFFERENT set of rows to a slot size someone
    else already decided, instead of duplicating this search.

    Tie-break, fixed 25-Aug-2026: whenever slot/n lands unclamped inside
    the band for more than one candidate n (common -- the band is often
    wide enough that both n and n+1 fit with ~0% deviation), EVERY one of
    those candidates scores as good as the others on deviation alone. The
    old code kept whichever was checked first in an unordered Python
    `set` -- an arbitrary tie-break that, for a lower share count (higher
    per-share price), regularly landed the recommended limit price ABOVE
    the stock's own last close (real example: KPITTECH closed 592,
    n=55 gave 590.53 -- at/below close -- and n=54 gave 601.47, a coin-
    flip tie on deviation, and the arbitrary order picked 601.47).
    Placing a "limit" buy above the last close defeats the point of a
    limit order -- it just chases the price up. On a tie (within 1e-6%),
    now prefer the candidate with the LOWER price.

    A same-day close cap was ALSO tried (25-Aug-2026) and REVERTED same
    day, on evidence, not aesthetics: even with no tie, a genuinely
    unique best fit (COFORGE closed 1892.80, only n=17 gave ~0%
    deviation) can still land a little above close, simply because the
    fixed slot doesn't divide evenly into that stock's price. Forcing
    every such case down to `close` looks tidier per order, but
    `entry_hi` is not an arbitrary ceiling -- `_compute_stock_entry_band`
    builds it as a real, two-sided, volatility-derived confidence
    interval (close +/- band_pct) for where the stock will plausibly
    trade over the Day-1/2/3 fill window. Truncating Day-1 to only the
    lower half of that interval throws away real fill opportunities the
    model itself says are legitimate. Measured effect, run through the
    real 3-stage entry-tracking mechanism against the canonical 13-cycle
    basket (`research/fill_realism_v6_3stage.py`): the close cap dropped
    the Day-1 fill rate from 61% to 37% and cost -2.6pt of realized
    return over 13 cycles, because everything that misses Day 1 falls
    through to Day 2's Parkinson-volatility re-quote, which is
    deliberately wide (calibrated for near-certain completion, not price
    precision) and landed those names at a worse average price than
    Day-1's tighter band ever would have. The tie-break above is a free
    win -- same deviation, strictly better price, no downside, so it
    stays. This was not: it traded a real, measured return cost for an
    aesthetic preference on a small minority of names, so it's reverted.
    `entry_hi` remains the true upper bound; `close` is not a ceiling.
    """
    close, lo, hi = r["close"], r["entry_lo"], r["entry_hi"]
    ideal_n = max(1, round(slot / close))
    best_local = None
    for n in {ideal_n, max(1, ideal_n - 1), ideal_n + 1}:
        # slot/n is the price that lands EXACTLY on target; if it's
        # outside the tradeable band, clamp to the nearest edge -- still
        # placeable, just no longer a perfect match.
        price = min(max(slot / n, lo), hi)
        # 01-Sep-2026: tick-round BEFORE computing deviation, not after --
        # the deviation and the tie-break below must judge the price the
        # investor will actually be able to place, not a hypothetical
        # unrounded one.
        price = _round_to_tick(price)
        dev = abs(n * price - slot) / slot * 100.0
        if (best_local is None or dev < best_local[2] - 1e-6
                or (abs(dev - best_local[2]) <= 1e-6 and price < best_local[1])):
            best_local = (n, price, dev)
    return best_local


def _resolve_shares_to_target(rows: list, slot_target: float) -> dict:
    """
    Solve whole-share counts for `rows` against an ALREADY-DECIDED
    slot_target -- e.g. one set by the full 10-name basket's priciest
    stock -- so a buys-only or holds-only subset (entry_tracking's BUY
    quotes, or the HOLD rebalance step) agrees with the same basket total
    instead of each independently deriving its own, narrower target from
    whatever subset it happens to see. Same return shape as
    _compute_min_portfolio_sizing.
    """
    valid_rows = [r for r in rows
                  if r.get("entry_hi") and r["entry_hi"] > 0
                  and r.get("entry_lo") and r["entry_lo"] > 0
                  and r.get("close") and r["close"] > 0]
    slots = getattr(config, "PORTFOLIO_SIZE", 10)
    max_allowed_dev = getattr(config, "ENTRY_MAX_WEIGHT_DEV_PCT", 10.0)
    if not valid_rows or not slot_target:
        return {"min_portfolio": round(slot_target * slots) if slot_target else 0,
                "slot_target": slot_target or 0, "max_dev_pct": 0.0,
                "total_invested": 0, "shares": {}, "unsatisfied": [], "capped": False}

    details, unsatisfied, max_dev = {}, [], 0.0
    for r in valid_rows:
        n, price, dev = _solve_shares_to_slot(slot_target, r)
        details[r["symbol"]] = (n, price, dev)
        max_dev = max(max_dev, dev)
        if dev > max_allowed_dev:
            unsatisfied.append(r["symbol"])

    shares_map = {}
    total_invested = 0.0
    for sym, (n, price, dev) in details.items():
        shares_map[sym] = {"shares": n, "limit_price": price,
                           "invested": n * price, "dev_pct": dev}
        total_invested += n * price

    return {
        "min_portfolio": round(slot_target * slots),
        "slot_target": slot_target,
        "max_dev_pct": max_dev,
        "total_invested": total_invested,
        "shares": shares_map,
        "unsatisfied": unsatisfied,
        "capped": False,
    }


def build_entry_sheet(expiry: date, session=None) -> dict:
    """
    The order sheet for the month starting after `expiry`.

    Produced on expiry evening, so the next session's open is unknown --
    entry is quoted as a band around the signal-day close.
    """
    import nse_client
    import scoring

    symbols = strategy.load_fo_universe()
    sectors = strategy.load_sector_map()
    decision = strategy.basket_for(expiry, symbols, sectors, session=session)
    hist = decision.hist
    basket, full = decision.table, decision.full
    signals = decision.full          # scored frame, indexed by symbol
    kept = decision.symbols
    dropped, added, veto_ran = (decision.veto_dropped, decision.veto_added,
                                decision.veto_ran)

    stop_pct_sheet = decision.stop_pct
    stop = stop_pct_sheet / 100.0
    target = config.V4_TARGET_PCT / 100.0

    # What are we holding right now? Read it straight from book.json (26-
    # Aug-2026 structural fix), not by asking daily_report.build() to
    # RE-DERIVE the whole outgoing month from scratch. Re-deriving means
    # re-running selection/ranking against today's data and asking
    # "what would the algorithm pick if run again today" -- not "what
    # was actually held" -- and those answers diverge the moment
    # something un-reconstructable changed a decision after the fact.
    # Concretely: KALYANKJIL was ASM-vetoed on 03-Aug and dropped from
    # the real basket, but ASM is only ever a CURRENT snapshot, so
    # re-deriving the 28-Jul basket today silently un-vetoes it -- and
    # ADANIGREEN, which WAS actually in the real basket, silently
    # vanishes, because the redo has no way to know it belonged there.
    # book.py (plus its archive) is the one place this was ever recorded
    # WITHOUT needing reconstruction -- written the moment a real fill or
    # a real close happens. See book.holdings_for_expiry's docstring.
    #
    # 25-Aug-2026 fix (carried forward): a data gap or StrategyError here
    # must not silently continue with current={}, which would render
    # every one of the 10 names as a fresh BUY -- including ones already
    # held, doubling real exposure and sending zero SELL orders for names
    # that should have been dropped. Let it propagate -- cmd_sheet's own
    # try/except already sends a real failure alert instead of a
    # silently-wrong one.
    import book as book_module
    import types
    # book.json tags each position with the governing expiry it was last
    # (re-)entered under, which for anything still live going into THIS
    # expiry evening is the PRIOR monthly expiry, not this one -- `expiry`
    # here names the sheet being produced (the month STARTING after it),
    # the outgoing month it needs to look up is one cycle earlier.
    outgoing_expiry = governing_expiry(expiry, strategy.known_trading_days())
    book_positions = book_module.holdings_for_expiry(outgoing_expiry)
    current = {}
    for sym, pos in book_positions.items():
        entry_px = pos["entry_price"]
        if pos.get("exit_price") is not None:
            last = pos["exit_price"]
        elif sym in signals.index:
            last = float(signals.at[sym, "close"])
        else:
            last = None
        pnl_pct = ((last - entry_px) / entry_px * 100.0) if last else None
        current[sym] = types.SimpleNamespace(
            symbol=sym, entry=entry_px, entry_date=pos.get("entry_date"),
            last=last, pnl_pct=pnl_pct)

    new_set = set(kept)
    to_hold = [s for s in kept if s in current]
    # A name already closed mid-cycle (stop/target, exit_price set) needs
    # no SELL instruction here -- there is nothing left to sell, and the
    # investor was already told about it the day it happened (daily_
    # report's own "Exits today" section). Only a still-live position
    # that simply dropped out of the new ranking needs a real order.
    to_sell = [s for s in current
              if s not in new_set and book_positions[s].get("exit_price") is None]

    rows = []
    for sym in kept:
        close = float(signals.at[sym, "close"]) if sym in signals.index else None
        if close is None or close <= 0:
            continue
        lo, hi, band_pct = _compute_stock_entry_band(sym, hist, close)

        if config.LLM_TARGET_ENABLED:
            import llm_judgment
            feat = llm_judgment.build_features(sym, expiry, hist, entry=close,
                                               signals=signals)
            tgt = llm_judgment.get_or_set_target(sym, close, expiry, feat)
        else:
            tgt = {"target_pct": config.V4_TARGET_PCT, "source": "flat",
                   "basis": f"config.V4_TARGET_PCT = {config.V4_TARGET_PCT:.0f}%"}
        target = tgt["target_pct"] / 100.0

        rows.append({
            "symbol": sym,
            "sector": sectors.get(sym, "Unclassified"),
            "close": close,
            "entry_lo": lo, "entry_hi": hi,
            "entry_band_pct": band_pct,
            "sl_lo": lo * (1 - stop), "sl_hi": hi * (1 - stop),
            "tgt_lo": lo * (1 + target), "tgt_hi": hi * (1 + target),
            "target_pct": tgt["target_pct"],
            "target_source": tgt["source"],
            "target_basis": tgt.get("basis", ""),
            "action": "HOLD" if sym in current else "BUY",
        })

    sells = []
    for sym in to_sell:
        h = current[sym]
        sells.append({"symbol": sym, "last": h.last, "pnl_pct": h.pnl_pct})

    # Slot target (25-Aug-2026 rewire -- coverage-scale k RETIRED. It was
    # meant to keep the quoted minimum basket in sync with a hold's real
    # accumulated share count, but it does this by inflating the ONE
    # shared slot every row (including unrelated names) gets solved
    # against -- and that let one hold's inflated ratio push a totally
    # unrelated anchor stock's own correct, by-construction fit (e.g.
    # POWERINDIA's natural 1-share/0%-dev answer) off to a worse whole-
    # share count purely because the shared slot had grown for someone
    # else's reason. Explicit instruction, restated plainly: (1) find the
    # new min basket -- one fresh, un-inflated calculation every cycle,
    # exactly what a brand-new investor gets; (2) new investors buy every
    # continuing name (still in the new basket) at market open, fresh
    # picks via the limit chain; (3) existing investors are just told the
    # min number for each hold -- top up to it if they're short, or hold
    # (never sold down) if they already have more. No scaling, no ratio,
    # one basket, "scale up as needed" covers everyone already holding
    # extra.
    max_dev = getattr(config, "ENTRY_MAX_WEIGHT_DEV_PCT", 10.0)
    ceiling = 500000
    slots = getattr(config, "PORTFOLIO_SIZE", 10)
    rows_by_symbol = {r["symbol"]: r for r in rows}

    base_sizing = _compute_min_portfolio_sizing(rows)
    slot_target = base_sizing["slot_target"]
    capped = base_sizing.get("capped", False)

    # Solve the FULL basket (holds + fresh buys) against this one slot in
    # a consistent pass -- a HOLD's whole-share minimum (for the top-up
    # calc below) comes from the exact same fit every fresh buy gets, not
    # a separate calculation.
    sizing = _resolve_shares_to_target(rows, slot_target)
    sizing["capped"] = capped

    for r in rows:
        sym_sizing = sizing["shares"].get(r["symbol"], {})
        r["rec_shares"] = sym_sizing.get("shares", 1)
        r["rec_invested"] = sym_sizing.get("invested", r["entry_hi"])
        r["rec_limit_price"] = sym_sizing.get("limit_price", r["entry_hi"])

    rebalance = _compute_hold_rebalance(rows, to_hold, sizing["slot_target"])

    return {"expiry": expiry, "rows": rows, "dropped": dropped,
            "veto_ran": veto_ran, "sells": sells, "holds": to_hold,
            "had_prior_book": bool(current), "stop_pct": stop_pct_sheet,
            "sizing": sizing, "rebalance": rebalance}


def _compute_hold_rebalance(rows: list, holds: list, slot_target: float) -> dict:
    """
    Top-up-only rebalance for names carrying over as a HOLD (25-Aug-2026:
    slot_target is now the plain, un-inflated min basket -- coverage-
    scale k retired, see build_entry_sheet's comment for why). Simple by
    design, per explicit instruction: solve this cycle's min share count
    for the name (the exact same `_solve_shares_to_slot` fit every fresh
    buy in the basket gets), then compare to what's actually held --
    below the min, top up to it; at or above, just hold (never sold down;
    "scale up as needed" covers anyone already holding extra). A hold is
    only ever sold for a genuine stop/target/rollover exit (unchanged,
    strategy.simulate_month's job), never to bring its weight down.

    A held name that needs a top-up is bought at Day-1's MARKET open,
    same session a fresh buy's Day-1 limit is quoted -- no limit chase,
    no gap-risk-abort: a top-up isn't a new position, the hold already
    carries full exposure to that name's moves, so neither rationale for
    the fresh-buy 2-stage chain applies here.
    """
    import book as book_module

    rows_by_symbol = {r["symbol"]: r for r in rows}
    out = {}

    for sym in holds:
        r = rows_by_symbol.get(sym)
        pos = book_module.get(sym)
        if r is None or r.get("close", 0) <= 0:
            out[sym] = {"status": "no_price_data"}
            continue
        if not pos or not pos.get("shares"):
            out[sym] = {"status": "no_book_record"}
            continue

        close = r["close"]
        shares = int(pos["shares"])
        value = shares * close
        n, _price, _dev = _solve_shares_to_slot(slot_target, r)
        target = max(n, shares)

        if target <= shares:
            dev = ((value - slot_target) / slot_target * 100.0) if slot_target else 0.0
            out[sym] = {"status": "ok", "shares": shares, "min_shares": n,
                        "value": value, "dev_pct": dev}
            continue

        new_value = target * close
        out[sym] = {
            "status": "rebalance", "action": "TOP-UP",
            "current_shares": shares, "current_value": value,
            "current_dev_pct": ((value - slot_target) / slot_target * 100.0) if slot_target else 0.0,
            "new_shares": target, "min_shares": n, "delta_shares": target - shares,
            "new_value": new_value,
            "new_dev_pct": ((new_value - slot_target) / slot_target * 100.0) if slot_target else 0.0,
            "price": close,
        }

    return out


def render_entry_sheet(sheet: dict) -> str:
    """
    Day-0 sheet for EXISTING investors (15-Aug-2026 redesign -- brought to
    the same clean numbered/INR-prefixed style as entry_tracking.
    render_new_investor_day0, retiring the old dev_pct-heavy, bullet-
    "MINIMUM PORTFOLIO GUIDE"-block version: "this looks like shit,
    fix it the way we did for NEW investors" -- explicit instruction).
    Three clean sections in fixed order (money out, then rebalance,
    then money in), each with its OWN numbering starting back at 1
    (25-Aug-2026 fix -- SELL/TOP-UP/BUY/CONTINUE TO HOLD are different
    order tickets, not one combined list, so a continuous count across
    all of them was actively misleading), a single SL/Exit line up top
    instead of repeating it per stock, and "INR"/"~" money formatting
    matching the new-investor message exactly.
    """
    from alerts import esc
    rows = sheet["rows"]
    sells = sheet.get("sells") or []
    holds = sheet.get("holds") or []
    buys = [r for r in rows if r.get("action") != "HOLD"]
    sizing = sheet.get("sizing") or {}
    stop_pct = sheet.get("stop_pct", config.V4_STOP_LOSS_PCT)

    targets = {r.get("target_pct", config.V4_TARGET_PCT) for r in rows}
    flat_targets = len(targets) <= 1

    L = [f"<b>{_entry_portfolio_title(sheet['expiry'])}</b>"]
    if sizing and sizing.get("min_portfolio"):
        L.append(f"Please place these orders immediately "
                 f"(minimum basket size: ~INR {_fmt_money(sizing['min_portfolio'])}, "
                 f"upscale as needed):")
    else:
        L.append("Please place these orders immediately:")
    L.append("")

    rebalance = sheet.get("rebalance") or {}
    rb_items = [(sym, rebalance.get(sym, {"status": "no_book_record"}))
                for sym in holds]
    needs_action = [(s, d) for s, d in rb_items if d.get("status") == "rebalance"]
    no_action = [(s, d) for s, d in rb_items if d.get("status") == "ok"]
    no_data = [(s, d) for s, d in rb_items
               if d.get("status") in ("no_book_record", "no_price_data")]

    # Money out first: exits at market, no limit chase -- the position is
    # already held, there is nothing to chase into. No P&L shown here any
    # more (15-Aug-2026 fix -- "we are already sending the other message
    # which shows our returns", the performance scorecard sent alongside
    # this one; repeating it here was redundant). Just the symbol now too
    # (15-Aug-2026 fix -- "no need to say dropped out of the basket").
    if sells:
        L.append("<b>SELL — market on open</b>")
        for i, s in enumerate(sells, 1):
            L.append(f"{i}. {esc(s['symbol'])}")
        L.append("")

    # SL/Exit line sits here now, right before the first section that
    # actually needs it (TOP-UP/BUY) -- SELL orders don't carry a
    # SL/Exit, so the line has nothing to do with them (15-Aug-2026 fix:
    # was above SELL before, misleadingly attached to it).
    if flat_targets:
        L.append(f"(SL: -{stop_pct:.0f}%, Exit: +{config.V4_TARGET_PCT:.0f}%)")
    else:
        L.append(f"(SL: -{stop_pct:.0f}% for every name; "
                 f"exit target varies by name, see below)")
    L.append("")

    # Rebalance next: also market, also no limit chase -- see
    # _compute_hold_rebalance's docstring (a top-up isn't new exposure).
    if needs_action:
        L.append("<b>TOP-UP — market on open</b>")
        for i, (sym, d) in enumerate(needs_action, 1):
            L.append(f"{i}. {esc(sym)}: Add {d['delta_shares']:,} more "
                     f"share{'s' if d['delta_shares'] != 1 else ''} "
                     f"(already held: {d['current_shares']:,})")
        L.append("")

    # Money in last: fresh limit buys, the 3-stage chain from tomorrow.
    if buys:
        L.append("<b>BUY — limit orders</b>")
        for i, r in enumerate(buys, 1):
            lp = r.get("rec_limit_price", r["entry_hi"])
            sh = r.get("rec_shares") or 1
            share_word = "share" if sh == 1 else "shares"
            suffix = (f" (Exit: +{r.get('target_pct', config.V4_TARGET_PCT):.0f}%)"
                      if not flat_targets else "")
            L.append(f"{i}. {esc(r['symbol'])}: {sh:,} {share_word} "
                     f"@ INR {_fmt_money(lp)}{suffix}")
        L.append("")

    if no_action:
        L.append("<b>CONTINUE TO HOLD — no order needed</b>")
        for i, (sym, d) in enumerate(no_action, 1):
            min_shares = d.get("min_shares", d["shares"])
            share_word = "share" if min_shares == 1 else "shares"
            L.append(f"{i}. {esc(sym)}: hold {min_shares:,} {share_word}")
        L.append("")

    if no_data:
        L.append("<b>CONTINUE TO HOLD — verify your own holding size</b>")
        for i, (sym, d) in enumerate(no_data, 1):
            L.append(f"{i}. {esc(sym)} — no book record on file, please "
                     f"check against the slot target above")
        L.append("")

    # 15-Aug-2026: dropped three lines the investor doesn't need --
    # the "(!) Could not fit within +/-10%" sizing-mechanics note
    # ("nobody wants to know"), the SL/target placement reminder
    # (explicit instruction to remove), and "Surveillance check did not
    # run" (an internal-algo detail, not something to surface to the
    # investor -- explicit instruction to remove). The "Excluded by
    # surveillance" list itself stays: that names an actual dropped
    # symbol, which the investor does need to know about.
    if not sheet.get("had_prior_book", True):
        L.append("<i>No prior positions found — treating every name above "
                 "as a fresh buy.</i>")
        L.append("")

    if sheet["dropped"]:
        L.append("<b>Excluded by surveillance:</b>")
        for sym, why in sheet["dropped"]:
            L.append(f"  {esc(sym)} — {esc(why)}")
    return "\n".join(L).strip()


EXIT_LABEL = {
    "STOP": "STOPLOSS",
    "TARGET": "Target exit",
}


def render(rpt: Report) -> str:
    """
    Telegram HTML, deliberately terse -- it is read on a phone before
    market open and every line should map to an action or a number.

    Four sections, each shown only when it has content:
        header · month to date · exits today · sell orders ·
        continue to hold

    Stream 2 only (14-Aug-2026 cleanup): this is the ongoing daily note,
    which never buys and never reports a momentum/rollover drop -- both
    belong to Stream 1 (entry_tracking.py's Day-0 through Day-3 fill
    messages, and build_entry_sheet/render_entry_sheet's expiry-evening
    sheet). A ROLLOVER exit is therefore deliberately excluded from
    "Exits today" below even on the one evening it would technically
    match `rpt.as_of` -- render_entry_sheet already announced it that
    same evening, so repeating it here would just be a second, later
    notice of the same decision.
    """
    from alerts import esc
    L = [f"<b>Momentum Tracker — {rpt.as_of:%d-%m-%y}</b>", ""]

    sign = "+" if rpt.mtd_return_pct >= 0 else ""
    L.append(f"<b>Cycle performance: {sign}{rpt.mtd_return_pct:.2f}% "
             f"since {rpt.expiry:%d-%m-%Y}</b>")
    L.append("")

    today_exits = [e for e in rpt.exits
                  if e.exit_date == rpt.as_of and e.reason != "ROLLOVER"]
    if today_exits:
        L.append("<b>Exits today</b>")
        for e in today_exits:
            s = "+" if e.pnl_pct >= 0 else ""
            L.append(f"{esc(e.symbol)} — {EXIT_LABEL.get(e.reason, e.reason)} "
                     f"@ {_fmt_money(e.exit_px)} ({s}{e.pnl_pct:.1f}%)")
        L.append("")

    if rpt.sell_orders:
        L.append("<b>SELL ORDERS - PLACE NOW</b>")
        for o in rpt.sell_orders:
            if o["kind"] == "TARGET":
                L.append(f"{esc(o['symbol'])} - TARGET - PLACE AT LIMIT "
                         f"{_fmt_money(o['limit'])}")
            else:
                L.append(f"{esc(o['symbol'])} - OFF MOMENTUM - AT MARKET")
        L.append("")

    if rpt.holdings:
        L.append("<b>CONTINUE TO HOLD</b>")
        # A name below entry gets its exact stop price and how far the
        # price still has to fall to reach it. The stop is the one fixed
        # on entry day -- 5% or 10% of the entry price depending on the
        # breadth regime -- so quoting a percentage would be ambiguous.
        # Only the rupee level is unambiguous at the broker terminal.
        losers = []
        for h in sorted(rpt.holdings, key=lambda x: -x.pnl_pct):
            s = "+" if h.pnl_pct >= 0 else ""
            # Telegram's Bot API HTML mode has no colour tag -- "in red" was
            # never going to render on any client. A marker emoji is the
            # actual substitute; the footnote below must match it.
            marker = "\U0001F534 " if h.pnl_pct < 0 else ""
            line = f"{marker}{esc(h.symbol)}  {_fmt_money(h.last)}  ({s}{h.pnl_pct:.1f}%)"
            if h.pnl_pct < 0 and h.stop:
                to_stop = (h.last - h.stop) / h.last * 100 if h.last else 0.0
                line += (f"  —  SL {_fmt_money(h.stop)}"
                         f" ({to_stop:.1f}% away)")
                losers.append(h)
            L.append(line)
        if losers:
            L.append("")
            L.append("<i>The names above marked \U0001F534 are below your "
                     "entry. Check a resting stop-loss order is live at the "
                     "exact price shown against each — it is the level set "
                     "on entry day and does not move.</i>")
        L.append("")

    if rpt.exited_review:
        L.append("<b>Exited</b>")
        for e in rpt.exited_review:
            x = f"{'+' if e['exit_pct'] >= 0 else ''}{e['exit_pct']:.1f}%"
            on = f" on {_ordinal_day(e['exit_date'])}" if e.get("exit_date") else ""
            if e["now_pct"] is None:
                L.append(f"{esc(e['symbol'])} (exited at {x}{on})")
                continue
            y = f"{'+' if e['now_pct'] >= 0 else ''}{e['now_pct']:.1f}%"
            L.append(f"{esc(e['symbol'])} (exited at {x}{on}, today at {y})")
        L.append("")

    # 25-Aug-2026 fix: rpt.flagged_actions was written to the ledger
    # (ledger.py persists it) but never actually shown here -- e.g. "no
    # price in today's bhavcopy -- carrying the previous close, stop not
    # evaluated" sat silently in a JSONL file while the holding above
    # rendered as an ordinary HOLD with a live-looking SL line. A stop
    # that isn't actually being checked is exactly the kind of thing that
    # must show up on the phone, not just in an audit trail.
    if rpt.flagged_actions:
        L.append("<b>⚠ Flagged</b>")
        for a in rpt.flagged_actions:
            L.append(esc(a))

    return "\n".join(L).strip()
