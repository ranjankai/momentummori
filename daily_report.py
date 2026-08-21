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


def build(as_of: date, session=None) -> Report:
    """
    Assemble the evening report for `as_of` (a trading day whose bhavcopy
    has been published). Raises strategy.StrategyError if the month
    cannot be reconstructed -- the caller turns that into a failure alert.
    """
    trading_days = strategy.known_trading_days()
    expiry = governing_expiry(as_of, trading_days)
    symbols = strategy.load_fo_universe()
    sectors = strategy.load_sector_map()

    # ONE basket decision, shared with the order sheet and the CLI.
    decision = strategy.basket_for(expiry, symbols, sectors, session=session)
    hist = decision.hist
    basket, full = decision.table, decision.full
    basket_symbols = decision.symbols
    ranked_order = decision.ranked_order
    veto_dropped, veto_ran = decision.veto_dropped, decision.veto_ran

    fwd = strategy.load_price_history(as_of, symbols, days=60)
    merged = dict(hist)
    merged.update(fwd)
    days = [d for d in sorted(merged) if expiry < d <= as_of]
    if not days:
        raise strategy.StrategyError(
            f"No trading days between governing expiry {expiry} and {as_of}")

    # ONE engine, shared with the backtest.
    #
    # carry_forward=True is deliberate: it marks still-open positions to
    # the last CLOSE, which is what "month to date" means for a book that
    # has not been sold. carry_forward=False takes the other branch and
    # force-sells at the final day's OPEN, which understated MTD by
    # 0.30pp when this was first wired.
    #
    # The re-marking that carry_forward does to carry_out does not reach
    # us: simulate_month snapshots open_positions with their ORIGINAL
    # cost bases before that branch runs.
    # Mid-month replacements are chosen on CASH data recomputed today,
    # never on the frozen expiry composite -- rollover's rank ordering is
    # static for three weeks between expiries. The LLM picks from the full
    # eligible list; None means nothing cleared the deployment hurdle and
    # the slot stays in cash.
    candidate_fn = None
    if config.LLM_CANDIDATE_ENABLED:
        import llm_judgment
        rs = llm_judgment.rs_rank(as_of, symbols, merged)

        def candidate_fn(eligible, _rs=rs):
            ordered = [s for s in _rs.index if s in set(eligible)]
            if not ordered:
                return None
            return llm_judgment.choose_candidate(ordered, _rs).get("symbol")

    stop_pct = strategy.resolve_stop_pct(expiry, symbols, hist)
    res = strategy.simulate_month(
        ranked_order, merged, days, sectors,
        basket_symbols=basket_symbols, carry_forward=True,
        stop_pct=stop_pct, candidate_fn=candidate_fn)
    # `res.to_buy` (mid-cycle replacement candidates) is NOT carried onto
    # the Report at all (14-Aug-2026 cleanup) -- Stream 2 (this daily note)
    # never buys, only Stream 1 (entry_tracking.py, expiry evening through
    # Day 3) does. A freed slot sits in cash till next cycle in production
    # anyway (config.V4_REDEPLOY_ENABLED = False), so this was always dead
    # weight here -- still used for `empty_slots`, which is informational.
    holdings, exits, to_buy, mtd = (res.open_positions, res.exits,
                                    res.to_buy, res.return_pct)

    rpt = Report(as_of=as_of, expiry=expiry, entry_date=days[0],
                 holdings=holdings, exits=exits,
                 mtd_return_pct=mtd,
                 empty_slots=config.PORTFOLIO_SIZE - len(holdings) - len(to_buy))

    # --- actionable orders -------------------------------------------------
    # A target sell is only accepted by the exchange once it is inside the
    # dynamic price band, so it is issued the evening it becomes placeable
    # rather than on entry day. Momentum/rollover sells (a stock dropping
    # out of the basket) are NOT reported here -- that only happens at the
    # monthly rebalance and is already reported that same evening by
    # build_entry_sheet/render_entry_sheet (Stream 1's Day-0 sheet), so
    # repeating it here the next evening would just be a second, later
    # notice of the same decision. (14-Aug-2026 cleanup: the old MOMENTUM
    # branch here read `rpt.to_sell`, a field this daily build() never
    # actually populated -- it was already dead code, just for the wrong
    # reason. Removed rather than fixed, since Stream 1 is the right home
    # for it, not this note.)
    for h in rpt.holdings:
        if h.target_placeable:
            rpt.sell_orders.append({
                "symbol": h.symbol, "kind": "TARGET",
                "limit": round(h.target, 2),
                "last": round(h.last, 2) if h.last else None,
            })

    # --- daily off-momentum judgement ----------------------------------
    # Mid-month only; expiry day is mechanical. Additive to the 5% stop:
    # it can bring a position out early and can do nothing else.
    if config.LLM_EXIT_ENABLED and rpt.holdings:
        import llm_judgment
        for h in rpt.holdings:
            try:
                feat = llm_judgment.build_features(h.symbol, as_of, merged,
                                                   entry=h.entry)
                held_days = len([x for x in days if x >= h.entry_date])
                v = llm_judgment.exit_judgement(h.symbol, feat, held_days,
                                                h.entry, h.stop)
            except Exception as exc:
                logger.error("Exit judgement failed for %s: %s", h.symbol, exc)
                continue
            if v.get("exit_now"):
                rpt.sell_orders.append({
                    "symbol": h.symbol, "kind": "OFF_MOMENTUM", "limit": None,
                    "reason": v.get("exit_reason", ""),
                    "confidence": v.get("confidence"),
                })

    # --- how have the exits aged? --------------------------------------
    # Mark every closed trade to today's close. If the stock is higher
    # now than where we sold, the exit was wrong -- and it should be
    # visible rather than quietly forgotten.
    today_frame = merged.get(as_of)
    for e in rpt.exits:
        now = None
        if today_frame is not None and e.symbol in today_frame.index:
            c = today_frame.at[e.symbol, "close_price"]
            if pd.notna(c) and c > 0:
                now = float(c)
        rpt.exited_review.append({
            "symbol": e.symbol,
            "reason": e.reason,
            "exit_pct": round(e.pnl_pct, 2),
            "now_pct": round((now - e.entry) / e.entry * 100, 2) if now else None,
            "exit_date": e.exit_date,
        })

    # Already computed above, BEFORE the simulation, so the walk used the
    # same ten names the veto left behind.
    rpt.veto_dropped = veto_dropped
    rpt.veto_ran = veto_ran
    return rpt


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
        sign = "+" if r["return_pct"] >= 0 else ""
        L.append(f"{names[int(m) - 1]} {y}: {sign}{r['return_pct']:.2f}%")

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
    """
    close, lo, hi = r["close"], r["entry_lo"], r["entry_hi"]
    ideal_n = max(1, round(slot / close))
    best_local = None
    for n in {ideal_n, max(1, ideal_n - 1), ideal_n + 1}:
        # slot/n is the price that lands EXACTLY on target; if it's
        # outside the tradeable band, clamp to the nearest edge -- still
        # placeable, just no longer a perfect match.
        price = min(max(slot / n, lo), hi)
        dev = abs(n * price - slot) / slot * 100.0
        if best_local is None or dev < best_local[2]:
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

    # What are we holding right now? Reconstruct the OUTGOING month (the
    # one ending at this expiry) so the sheet can tell HOLD from SELL
    # from BUY. Without this the sheet lists all ten names as buys,
    # including ones already owned, and never says what to exit.
    current = {}
    try:
        outgoing = build(expiry, session=session)
        current = {h.symbol: h for h in outgoing.holdings}
    except Exception as exc:
        logger.warning("Could not reconstruct the outgoing month (%s); "
                       "treating every name as a fresh buy", exc)

    new_set = set(kept)
    to_hold = [s for s in kept if s in current]
    to_sell = [s for s in current if s not in new_set]

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

    # Slot target (14-Aug-2026 rewire -- "we will not sell held stocks...
    # nobody said we can't buy more", explicit instruction; retires
    # _slot_target_from_holds' interval-stabbing anchor and
    # _compute_hold_rebalance's trim-only asymmetry together, see
    # BACKTEST_LOG.md's 14-Aug-2026 "coverage-scale" section for the full
    # derivation and the research validation this mirrors exactly):
    #
    # 1. Build a fresh, +/-max_dev%-consistent minimum basket exactly as
    #    a brand-new investor gets -- priciest pick's own band-low sets
    #    the slot, every name in the FULL basket solved to whole shares
    #    against it.
    # 2. Coverage scale: k = max(held_shares / this-basket's-own-share-
    #    count) over every hold, using the RATIO not the raw share count
    #    (a cheap stock can carry a huge raw count without being the
    #    binding constraint -- found via IDEA vs KAYNES in the 2025-09
    #    backtest cycle). Scaling the whole basket by k guarantees, by
    #    construction, every hold's target share count is >= what's
    #    already held.
    # 3. Feasibility floor, generalised to ALL ten names now (holds
    #    included, was fresh-buys-only before 14-Aug-2026): if any single
    #    name's own band-low still can't fit inside the coverage-scaled
    #    slot, that price becomes the floor instead. Safe regardless of
    #    order -- raising the slot only ever grows every name's share
    #    count, so it can only make step 2's guarantee MORE generous.
    # A hold is NEVER sold to bring its weight down -- only a genuine
    # stop/target/rollover exit still sells one (unchanged, strategy.
    # simulate_month's job, not this sheet's).
    max_dev = getattr(config, "ENTRY_MAX_WEIGHT_DEV_PCT", 10.0)
    ceiling = 500000
    slots = getattr(config, "PORTFOLIO_SIZE", 10)
    rows_by_symbol = {r["symbol"]: r for r in rows}

    import book as book_module

    base_sizing = _compute_min_portfolio_sizing(rows)
    base_slot = base_sizing["slot_target"]
    base_shares = {sym: s["shares"] for sym, s in base_sizing["shares"].items()}

    if to_hold:
        ratios = []
        for sym in to_hold:
            pos = book_module.get(sym)
            if pos and pos.get("shares") and base_shares.get(sym):
                ratios.append(pos["shares"] / base_shares[sym])
        k = max(ratios) if ratios else 1.0
    else:
        k = 1.0
    slot_target = base_slot * k

    floor = max((r["entry_lo"] for r in rows if r.get("entry_lo")), default=0)
    slot_target = max(slot_target, floor)
    capped = False
    if slot_target * slots > ceiling:
        capped = True
        slot_target = ceiling / slots

    # Solve the FULL basket (holds + fresh buys) against the final slot in
    # one consistent pass -- a HOLD's whole-share TARGET (for the top-up
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
    Top-up-only rebalance for names carrying over as a HOLD (14-Aug-2026
    rewire -- retires the trim-only asymmetry and _slot_target_from_
    holds' interval-stabbing anchor together: "we will not sell held
    stocks... nobody said we can't buy more" (explicit instruction).
    Trimming an overweight hold for rebalancing purposes is retired
    entirely -- a hold is only ever sold for a genuine stop/target/
    rollover exit (unchanged, strategy.simulate_month's job), never to
    bring its weight down. See BACKTEST_LOG.md's 14-Aug-2026 "coverage-
    scale" section for the full design and the POWERINDIA/KAYNES-style
    overweight cases this replaces.

    `slot_target` (build_entry_sheet's caller) has ALREADY been raised by
    the coverage-scale + feasibility-floor logic to be big enough that
    every hold's real share count fits inside it -- so this function's
    only job is to solve each hold's whole-share TARGET against that
    slot, the exact same `_solve_shares_to_slot` fit every fresh buy in
    the basket gets, and report the gap to buy. The gap should never be
    negative by construction (that guarantee lives in build_entry_sheet's
    coverage-scale step); `max(target, shares)` below is belt-and-
    suspenders against a stray rounding edge case, not the mechanism
    that makes this safe.

    A held name is bought at Day-1's MARKET open, same session a fresh
    buy's Day-1 limit is quoted -- no limit chase, no gap-risk-abort: a
    top-up isn't a new position, the hold already carries full exposure
    to that name's moves, so neither rationale for the fresh-buy 2-stage
    chain applies here.
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
            out[sym] = {"status": "ok", "shares": shares, "value": value, "dev_pct": dev}
            continue

        new_value = target * close
        out[sym] = {
            "status": "rebalance", "action": "TOP-UP",
            "current_shares": shares, "current_value": value,
            "current_dev_pct": ((value - slot_target) / slot_target * 100.0) if slot_target else 0.0,
            "new_shares": target, "delta_shares": target - shares,
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
    then money in), one continuous numbering across all of them, a
    single SL/Exit line up top instead of repeating it per stock, and
    "INR"/"~" money formatting matching the new-investor message exactly.
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

    i = 0
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
        for s in sells:
            i += 1
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
        for sym, d in needs_action:
            i += 1
            L.append(f"{i}. {esc(sym)}: Add {d['delta_shares']:,} more "
                     f"share{'s' if d['delta_shares'] != 1 else ''} "
                     f"(already held: {d['current_shares']:,})")
        L.append("")

    # Money in last: fresh limit buys, the 3-stage chain from tomorrow.
    if buys:
        L.append("<b>BUY — limit orders</b>")
        for r in buys:
            i += 1
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
        for sym, d in no_action:
            L.append(f"{esc(sym)}: {d['shares']:,} shares, on target")
        L.append("")

    if no_data:
        L.append("<b>CONTINUE TO HOLD — verify your own holding size</b>")
        for sym, d in no_data:
            L.append(f"{esc(sym)} — no book record on file, please check "
                     f"against the slot target above")
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
    L.append(f"<b>Cycle performance: {sign}{rpt.mtd_return_pct:.2f}%</b>")
    L.append(f"<i>since the {rpt.expiry:%d-%m-%y} expiry</i>")
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

    return "\n".join(L).strip()
