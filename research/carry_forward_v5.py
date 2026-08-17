"""
Compares two whole-share, compounding portfolios across the same 16 real
cycles, both built on top of the validated V5 2-stage fill mechanism
(research/fill_realism_v5.py) and strategy.simulate_month (so stop/target/
rollover exits are the same well-tested engine used everywhere else --
do not fork the loop):

  PURE IN-AND-OUT   -- every slot is sold in full and rebought fresh via
                       the V5 fill chain every month, even if the same
                       name repeats. No carry, no rebalancing. This is
                       the same assumption research/fill_realism_v5.py
                       already backtests, just chained into one
                       compounding NAV instead of 16 independent cycles.

  CARRY-FORWARD     -- names that repeat in the new basket are HELD, and
                       NEVER SOLD for rebalancing (explicit instruction,
                       14-Aug-2026: "we will not sell held stocks...
                       nobody said we can't buy more"). The slot target
                       is a fresh, +/-10%-consistent minimum basket (same
                       construction a brand-new investor gets) scaled UP
                       just enough that every hold's current share count
                       already fits inside it, floored further if any
                       single name's own band-low still can't fit even 1
                       share. Every hold is then topped up to that target
                       with an unconditional Day-1 MARKET buy -- never
                       trimmed, whether underweight OR overweight. Only
                       names actually dropping out of the basket, or a
                       slot freed by a stop/target exit, go through a
                       fresh V5 2-stage buy. (Superseded the interval-
                       stabbing target + trim-only rebalance from
                       13-Aug-2026 -- see BACKTEST_LOG.md's 14-Aug-2026
                       section.)

Both scenarios call strategy.simulate_month with carry_forward=True so
`.exits`/`.carry` always populate (needed for whole-share mark-to-market)
-- the PURE IN-AND-OUT scenario just never hands its own `.carry` back in
as next month's `carry_in`, forcing every slot to be re-bought from
scratch regardless of what simulate_month itself would have carried.

CASH ACCOUNTING
---------------
A single cash balance plus a whole-share book. Sells/exits/trims add
cash at their real fill price; buys/top-ups subtract it. NAV = cash +
mark-to-market of whatever's still held. Month 1 seeds cash with that
cycle's own min-portfolio total (the same number a fresh investor would
be told to deploy) so both scenarios start from an identical base.

OUTPUT
------
One JSON line per scenario to data/carry_forward_v5.json (both scenarios'
full monthly detail plus the two final NAVs), printed as a summary table.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import config
import daily_report
import harness
import strategy

Z80 = 0.8416212
MAX_DEV = getattr(config, "ENTRY_MAX_WEIGHT_DEV_PCT", 10.0)
SLOTS_N = getattr(config, "PORTFOLIO_SIZE", 10)
CEILING = 500000
OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "carry_forward_v5.json")


def parkinson_sigma(row):
    h = float(row.get("high_price", 0) or 0)
    l = float(row.get("low_price", 0) or 0)
    if h <= 0 or l <= 0 or h < l:
        return None
    if h == l:
        return 0.0
    return math.log(h / l) / (2.0 * math.sqrt(math.log(2)))


def new_investor_minimum(y, m):
    """
    The TRUE new-investor minimum basket for cycle (y, m) -- independent
    of any account history, exactly what daily_report.build_entry_sheet /
    entry_tracking would quote a brand-new investor that month: the
    priciest of the full 10-name basket's own band-low price x 10 slots.

    NOT the same thing as a pure-in-and-out backtest's actual cash
    balance that month (an earlier version of the comparison table
    conflated the two -- the backtest's cash reflects THAT account's own
    compounding history, which has nothing to do with what a fresh
    investor joining this month alone would need).

    Also runs the V5 2-stage fill against all 10 names (a new investor
    always buys the whole basket fresh) purely to report days_to_fill.
    """
    ex, nx = harness.cycle_dates(y, m)
    picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=SLOTS_N)
    merged = harness.prices(ex, nx)
    after = sorted(d for d in merged if d > ex)
    entry_day, day2 = after[0], after[1]

    hist = strategy.load_price_history(ex, harness.universe())
    hist_dates = sorted(hist.keys())
    sig_frame = hist.get(ex) if ex in hist else hist.get(hist_dates[-1])
    rows = []
    for sym in picks:
        if sig_frame is None or sym not in sig_frame.index:
            continue
        close = float(sig_frame.loc[sym, "close_price"])
        if close <= 0:
            continue
        lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
        rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

    sizing = daily_report._compute_min_portfolio_sizing(rows)
    slot_target = sizing["slot_target"]

    days_to_fill = 0
    for sym, s in sizing["shares"].items():
        row1 = merged[entry_day].loc[sym] if sym in merged[entry_day].index else None
        if row1 is None:
            continue
        day1_open = float(row1.get("open_price", 0) or 0)
        low1 = float(row1.get("low_price", 0) or 0)
        if day1_open <= 0:
            continue
        if low1 > 0 and low1 <= s["limit_price"]:
            days_to_fill = max(days_to_fill, 1)
            continue
        anchor_stop = day1_open * (1 - stop_pct / 100.0)
        if low1 > 0 and low1 <= anchor_stop:
            continue  # aborted, not "still filling"
        sigma1 = parkinson_sigma(row1)
        day1_close = float(row1.get("close_price", 0) or 0)
        if sigma1 is None or day1_close <= 0:
            continue
        p80 = day1_close * math.exp(Z80 * sigma1)
        if day2 not in merged or sym not in merged[day2].index:
            continue
        row2 = merged[day2].loc[sym]
        open2 = float(row2.get("open_price", 0) or 0)
        if open2 <= 0 or open2 <= anchor_stop:
            continue
        days_to_fill = max(days_to_fill, 2)

    return {"month": f"{y}-{m:02d}", "min_balance": round(sizing["min_portfolio"], 2),
           "slot_target": round(slot_target, 2), "days_to_fill": days_to_fill,
           "unsatisfied": sizing["unsatisfied"]}


def existing_basket_at_fill(alloc: dict, prev_final_book: dict) -> dict:
    """
    The existing/carry-forward investor's basket at the moment every slot
    for this cycle is resolved -- continuing HOLDs (refreshed to their
    price per strategy.simulate_month's carry-out marking, i.e. `final`'s
    OPEN, matching book.py, see BACKTEST_LOG.md 14-Aug-2026) plus this
    month's fresh BUYs (their real simulated fill price/day). This is the
    one and only source of REAL prices in the New-vs-Existing comparison
    from here on -- New no longer computes anything of its own, see
    new_investor_from_existing below.

    `prev_final_book` is the PRIOR month's `alloc["final_book"]` (or {}
    for the very first cycle) -- that is where a HOLD's refreshed price
    actually lives, since `alloc["holds_before"]` only has share counts.

    A HOLD's share count is its POST-top-up count (`alloc["topups"]`,
    14-Aug-2026 rewire), not `holds_before`'s pre-cycle snapshot -- topped
    -up shares are real, bought at Day-1's open, same as `holds_before`'s
    survivors. The PRICE stays `prev_final_book`'s mark, unchanged: that
    mark is last cycle's carry-out at `final`'s open, which IS this
    cycle's Day-1 open (the same arrival-price identity that makes the
    Day-1-market top-up correct in the first place) -- so it's already
    the same number the top-up itself was bought at, no separate lookup
    needed.
    """
    holds_before = alloc["holds_before"]
    sells, buys, picks = alloc["sells"], alloc["buys"], set(alloc["picks"])
    topups = alloc.get("topups", {})
    basket = {}
    for sym, shares in holds_before.items():
        if sym in picks and sym not in sells and sym not in buys:
            prev = prev_final_book.get(sym)
            if not prev or not prev.get("mark_price"):
                continue
            final_shares = topups[sym]["to_shares"] if sym in topups else shares
            basket[sym] = {"shares": final_shares, "price": prev["mark_price"], "status": "HOLD"}
    for sym, b in buys.items():
        basket[sym] = {"shares": b["shares"], "price": b["price"], "status": "BUY"}
    return basket


def new_investor_from_existing(existing_basket: dict) -> dict:
    """
    New investor's MINIMUM basket, derived from the existing investor's
    real basket rather than computed independently (agreed 14-Aug-2026 --
    an independently-solved price for New was the root cause of New and
    Existing quoting two different prices for the identical stock on the
    identical day, which is not something two real subscribers should
    ever see). Every price here IS the existing investor's real price,
    copied exactly -- no band search, no fill simulation of New's own.

    Mechanism: scale = 1 / (smallest share count in existing_basket), so
    that stock lands at exactly 1 share and everything else shrinks by
    the identical ratio, rounded. Safe by construction -- since it's the
    SMALLEST count, every other stock's scaled count is >= 1, never zero.

    A DIFFERENT investor wanting to deploy some other amount V is NOT
    this basket scaled again -- re-derive with scale = V / existing_total
    against `existing_basket` directly, so spending exactly what Existing
    has reproduces Existing's basket exactly (scale=1), with no
    compounded rounding loss from chaining through this minimum basket.
    """
    if not existing_basket:
        return {"min_portfolio": 0, "scale": 0, "shares": {}}
    min_shares = min(v["shares"] for v in existing_basket.values())
    scale = 1.0 / min_shares
    shares = {}
    total = 0.0
    for sym, v in existing_basket.items():
        n = max(1, round(v["shares"] * scale))
        shares[sym] = {"shares": n, "price": v["price"], "invested": n * v["price"],
                       "status": v["status"]}
        total += n * v["price"]
    return {"min_portfolio": round(total), "scale": scale, "shares": shares}


def run_scenario(cycles, carry_forward_scenario: bool, state: dict = None):
    """
    `state`, if given, resumes from a previous run_scenario call's
    returned state (book/cash/seed_capital/carry_in/nav_prev) -- lets a
    16-month run be split across several process calls that each stay
    under a tool-call timeout, without changing any of the economics.
    """
    state = state or {}
    book = state.get("book", {})          # symbol -> whole shares
    cash = state.get("cash")
    seed_capital = state.get("seed_capital")
    carry_in = state.get("carry_in", {})   # Position dict, fed forward only if carry_forward_scenario
    nav_prev = state.get("nav_prev")
    total_contributed = state.get("total_contributed", 0.0)
    monthly = []

    for (y, m) in cycles:
        ex, nx = harness.cycle_dates(y, m)
        picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=SLOTS_N)
        merged = harness.prices(ex, nx)
        after = sorted(d for d in merged if d > ex)
        if len(after) < 2:
            continue
        entry_day, day2 = after[0], after[1]
        after_nx = [d for d in sorted(merged) if d > nx]
        roll = after_nx[0] if after_nx else nx
        hold_dates = [d for d in sorted(merged) if ex < d <= roll]

        hist = strategy.load_price_history(ex, harness.universe())
        hist_dates = sorted(hist.keys())
        sig_frame = hist.get(ex) if ex in hist else hist.get(hist_dates[-1])

        rows = []
        for sym in picks:
            if sig_frame is None or sym not in sig_frame.index:
                continue
            close = float(sig_frame.loc[sym, "close_price"])
            if close <= 0:
                continue
            lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
            rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})
        rows_by_symbol = {r["symbol"]: r for r in rows}

        # Full allocation detail for this month, for reporting/audit --
        # avoids ever needing to re-run the backtest just to answer "what
        # did the portfolio actually hold in month X" (feedback 13-Aug-2026).
        alloc = {"holds_before": dict(book), "sells": {}, "topups": {}, "buys": {}}

        if not carry_forward_scenario:
            # Force-liquidate whatever survived from last month, always --
            # this scenario never carries anything.
            for sym in list(book.keys()):
                row1 = merged[entry_day].loc[sym] if sym in merged[entry_day].index else None
                px = (float(row1["open_price"]) if row1 is not None
                     and row1.get("open_price", 0) and row1["open_price"] > 0
                     else rows_by_symbol.get(sym, {}).get("close", 0))
                alloc["sells"][sym] = {"shares": book[sym], "price": round(px, 2),
                                       "proceeds": round(book[sym] * px, 2)}
                cash = (cash or 0) + book[sym] * px
                del book[sym]
            holds = []
        else:
            holds = [s for s in book if s in set(picks)]

        buys = [s for s in picks if s not in book]

        # --- slot target (14-Aug-2026, rewired -- "we will not sell held
        # stocks... nobody said we can't buy more"): build a fresh,
        # +/-10%-consistent minimum basket exactly as a brand-new investor
        # would get -- priciest pick's own band-low sets the slot, every
        # name in the FULL basket (holds + fresh) solved to whole shares
        # against it (daily_report._compute_min_portfolio_sizing) -- then
        # scale that basket UP just enough that every currently-held
        # name's REAL share count already fits inside it. Never down. The
        # old interval-stabbing target + trim-only rebalance (13-Aug-2026)
        # is retired entirely, including the overweight trim -- a hold is
        # never sold for rebalancing again, only for a genuine stop/
        # target/rollover exit (unchanged, still simulate_month's job).
        base_sizing = daily_report._compute_min_portfolio_sizing(rows)
        base_slot = base_sizing["slot_target"]
        base_shares = {sym: s["shares"] for sym, s in base_sizing["shares"].items()}

        if holds:
            # Coverage scale: the SMALLEST basket-multiple of the fresh
            # minimum that still covers every hold's current share count.
            # Uses the RATIO (held / this month's fresh-basket share
            # count for that name), not the raw held share count -- a
            # cheap stock can carry a huge raw count without being the
            # binding constraint. Found via IDEA (386 held, cheap, huge
            # base count) vs KAYNES (expensive, base count 1) in the
            # 2025-09 cycle -- see BACKTEST_LOG.md's 14-Aug-2026 section.
            ratios = [book[s] / base_shares[s] for s in holds
                     if base_shares.get(s) and s in book]
            k = max(ratios) if ratios else 1.0
        else:
            k = 1.0
        slot_target = base_slot * k

        # Buy-feasibility floor -- generalised to EVERY name in the
        # basket now (holds included), not just fresh buys: if any name's
        # own band-low still can't fit inside the coverage-scaled slot,
        # that price becomes the floor instead. Safe regardless of order
        # -- raising the slot only ever grows every other name's share
        # count too, so it can only make the coverage guarantee above
        # MORE generous, never less.
        floor = max((r["entry_lo"] for r in rows if r.get("entry_lo")), default=0)
        slot_target = max(slot_target, floor)
        if slot_target * SLOTS_N > CEILING:
            slot_target = CEILING / SLOTS_N

        if cash is None:
            cash = slot_target * SLOTS_N  # seed month 1's capital
            seed_capital = cash

        # --- resolve whole-share targets for the FULL basket against the
        # final slot_target -- holds and fresh buys on one consistent
        # basis. Safety clamp: a hold's target never falls below what's
        # already held, even from a stray rounding edge case -- belt-and-
        # suspenders on top of the mathematical guarantee above.
        full_sizing = daily_report._resolve_shares_to_target(rows, slot_target)
        target_shares = {sym: s["shares"] for sym, s in full_sizing["shares"].items()}
        for sym in holds:
            if sym in target_shares:
                target_shares[sym] = max(target_shares[sym], book[sym])

        # --- top up every hold to its target: unconditional Day-1 MARKET
        # buy (14-Aug-2026, explicit instruction). A top-up isn't a new
        # position -- the hold already carries full exposure to that
        # name's price moves, so there's no "wait for a dip" rationale
        # and no gap-risk-abort case (that protects against taking on
        # FIRST-time exposure into a falling knife, which doesn't apply
        # here). Buying at Day-1's open also matches simulate_month's own
        # carry-forward re-mark exactly (the arrival-price fix): `final`
        # from last cycle IS this cycle's Day-1, so the already-held
        # shares are already re-based to that same open -- the whole
        # position (old + new shares) lands on one consistent price,
        # same day, no blended cost basis.
        for sym in holds:
            target = target_shares.get(sym)
            if target is None:
                continue
            gap = target - book[sym]
            if gap <= 0:
                continue
            row1 = merged[entry_day].loc[sym] if sym in merged[entry_day].index else None
            day1_open = float(row1.get("open_price", 0) or 0) if row1 is not None else 0.0
            if day1_open <= 0:
                continue   # no usable Day-1 open -- leave the hold as-is this cycle
            alloc["topups"][sym] = {"from_shares": book[sym], "to_shares": target,
                                    "added": gap, "price": round(day1_open, 2),
                                    "cost": round(gap * day1_open, 2)}
            cash -= gap * day1_open
            book[sym] = target

        # --- V5 2-stage fill for the buys ---
        buy_rows = [r for r in rows if r["symbol"] in buys]
        sizing = daily_report._resolve_shares_to_target(buy_rows, slot_target)
        entry_overrides = {}
        # Days actually needed to get every slot resolved this month --
        # 0 if there were no empty slots to fill at all (a carry-forward
        # month where everything held), 1 if every buy cleared on Day 1's
        # limit, 2 if at least one needed the Day-2 mandatory buy. Aborts
        # (gap risk) don't extend this -- the slot is deliberately left
        # in cash, not still "being worked."
        days_to_fill = 0
        for sym in buys:
            s = sizing["shares"].get(sym)
            row1 = merged[entry_day].loc[sym] if sym in merged[entry_day].index else None
            if s is None or row1 is None:
                entry_overrides[sym] = None
                continue
            day1_open = float(row1.get("open_price", 0) or 0)
            low1 = float(row1.get("low_price", 0) or 0)
            if day1_open <= 0:
                entry_overrides[sym] = None
                continue
            anchor = day1_open
            if low1 > 0 and low1 <= s["limit_price"]:
                fill_price = min(day1_open, s["limit_price"])
                entry_overrides[sym] = (fill_price, entry_day, anchor)
                book[sym] = s["shares"]
                cash -= s["shares"] * fill_price
                days_to_fill = max(days_to_fill, 1)
                alloc["buys"][sym] = {"shares": s["shares"], "price": round(fill_price, 2),
                                      "cost": round(s["shares"] * fill_price, 2), "fill_day": 1}
                continue
            anchor_stop = anchor * (1 - stop_pct / 100.0)
            if low1 > 0 and low1 <= anchor_stop:
                entry_overrides[sym] = None
                continue
            sigma1 = parkinson_sigma(row1)
            day1_close = float(row1.get("close_price", 0) or 0)
            if sigma1 is None or day1_close <= 0:
                entry_overrides[sym] = None
                continue
            p80 = day1_close * math.exp(Z80 * sigma1)
            n2 = max(1, round(slot_target / p80))
            if day2 not in merged or sym not in merged[day2].index:
                entry_overrides[sym] = None
                continue
            row2 = merged[day2].loc[sym]
            open2 = float(row2.get("open_price", 0) or 0)
            if open2 <= 0 or open2 <= anchor_stop:
                entry_overrides[sym] = None
                continue
            entry_overrides[sym] = (open2, day2, anchor)
            book[sym] = n2
            days_to_fill = max(days_to_fill, 2)
            cash -= n2 * open2
            alloc["buys"][sym] = {"shares": n2, "price": round(open2, 2),
                                  "cost": round(n2 * open2, 2), "fill_day": 2}

        # --- authoritative mid-month walk (stops/targets/rollover) ---
        result = strategy.simulate_month(
            list(picks), merged, hold_dates, harness.sectors(),
            basket_symbols=list(picks), top_n=SLOTS_N, stop_pct=stop_pct,
            target_pct=None, carry_forward=True,
            carry_in=(carry_in if carry_forward_scenario else {}),
            entry_overrides=entry_overrides)

        alloc["exits"] = {}
        for e in result.exits:
            if e.symbol in book:
                alloc["exits"][e.symbol] = {"reason": e.reason, "price": round(e.exit_px, 2),
                                            "shares": book[e.symbol],
                                            "proceeds": round(book[e.symbol] * e.exit_px, 2),
                                            "pnl_pct": round(e.pnl_pct, 2)}
                cash += book[e.symbol] * e.exit_px
                del book[e.symbol]

        # --- corporate-action share adjustment (14-Aug-2026) ---
        # simulate_month's internal walk neutralises a split/bonus by
        # scaling PRICES so a FIXED share count stays value-consistent on
        # the OLD (pre-action) basis -- correct for its own percentage-
        # return bookkeeping, but this ledger tracks REAL, whole,
        # tradeable shares against RAW market prices everywhere else
        # (rows_by_symbol's signal close, the trim/rebalance step above,
        # every later cycle's own close). A share count carried through a
        # real split unadjusted, then combined with a raw price next
        # cycle, understates the position by the split ratio. Confirmed
        # via BSE's 23-May-2025 ~3:1 split: book stayed at 2 shares while
        # the internally-adjusted mark (Rs 7,417.51) was ~3x the real raw
        # open that day (Rs 2,472.50). Exited symbols are already gone
        # from book above and need no adjustment; only what's still held
        # carries forward and needs to move onto the real basis.
        alloc["corp_actions"] = {}
        for sym, fac in (result.corp_action_factors or {}).items():
            if sym in book and fac and fac != 1.0:
                old_shares = book[sym]
                new_shares = max(1, round(old_shares * fac))
                alloc["corp_actions"][sym] = {"factor": round(fac, 4),
                                              "from_shares": old_shares,
                                              "to_shares": new_shares}
                book[sym] = new_shares
                # If this SAME symbol was also bought or topped up THIS
                # cycle (both recorded earlier, before the split was
                # detected), those records are now stale -- still showing
                # the pre-split share count while `book`/`final_book` show
                # the post-split one. Found via BSE's Apr-2025 fresh buy:
                # alloc["buys"]["BSE"]["shares"] stayed at 2 while
                # final_book showed 6, so anything reading buys/topups
                # directly (existing_basket_at_fill, P&L reports) paired a
                # pre-split share count with a post-split price and
                # invented a phantom ~62% loss that never happened.
                # `cost`/real cash spent never changes -- only re-express
                # it against the corrected share count, so `shares*price`
                # still equals the same real cost (first cut of this fix,
                # 14-Aug-2026, updated ONLY `shares` and left `price` at
                # its pre-split level, which overstated invested value by
                # the same factor in the other direction -- caught by the
                # Apr-2025 additive sum swinging from +1.46% to -16.10%,
                # an implausibly large move for what should be a purely
                # cosmetic share-count relabelling).
                # `existing_basket_at_fill` (the one consumer that matters
                # here) reads a HOLD's PRICE from last cycle's final_book
                # mark, never from `topups[sym]["price"]" -- so only
                # `to_shares` needs correcting for topups. A BUY's price
                # IS read directly though, so `shares` and `price` must be
                # corrected TOGETHER, re-expressing the same real `cost`
                # (unchanged -- real cash already spent) against the
                # corrected share count.
                if sym in alloc["buys"]:
                    b = alloc["buys"][sym]
                    b["shares"] = new_shares
                    b["price"] = round(b["cost"] / new_shares, 4) if new_shares else b["price"]
                if sym in alloc["topups"]:
                    alloc["topups"][sym]["to_shares"] = new_shares

        # `result.carry` is ALWAYS needed here for this month's own NAV
        # mark, regardless of scenario -- it's what's still open at
        # month-end. `carry_in` (fed to NEXT month's simulate_month call)
        # is the separate, scenario-gated decision of whether that gets
        # handed forward or force-liquidated at next month's open. Using
        # the reset-to-empty `carry_in` for THIS month's mark (a bug in
        # an earlier version of this script) always priced pure-in-and-
        # out's still-open positions at zero, corrupting every NAV.
        month_end_carry = result.carry
        carry_in = result.carry if carry_forward_scenario else {}

        # A carried Position's `.entry` is re-based by simulate_month's own
        # carry-forward marking step to `final`'s OPEN (updated 14-Aug-2026
        # -- was the close until today; `final` IS next cycle's own Day-1,
        # so this now matches the open-anchored arrival price a fresh buy
        # gets that same day). That basis is simulate_month's INTERNAL,
        # corporate-action-adjusted price though -- book's share counts
        # are now on the REAL basis (adjusted just above), so marking them
        # with `.entry` would double-count any split detected this cycle.
        # Use `merged`'s RAW, unadjusted price at the same date instead --
        # `merged` is this script's own copy, never touched by
        # simulate_month's internal adjust_holding_window call (that
        # function always returns a NEW dict), so it's still the real,
        # quotable market price throughout (14-Aug-2026 fix, see
        # BACKTEST_LOG.md's corporate-action section).
        final_frame = merged.get(hold_dates[-1])

        def _raw_open(sym):
            if final_frame is not None and sym in final_frame.index:
                px = final_frame.at[sym, "open_price"]
                try:
                    px = float(px)
                except (TypeError, ValueError):
                    px = None
                if px and px == px and px > 0:   # px == px excludes NaN
                    return px
            carried = month_end_carry.get(sym)
            return carried.entry if carried else None

        # --- capital contribution (14-Aug-2026, explicit instruction:
        # "simply assume the min existing basket is satisfied every
        # month") -- rather than let cash go negative (implying
        # unmodelled margin/leverage), whatever shortfall this cycle's
        # buys/topups created is treated as fresh capital the investor
        # contributed to fully fund the min basket, exactly as a real
        # investor topping up their account would do. Cash is floored at
        # zero; the contribution is tracked separately and added to the
        # month's RETURN BASE (not counted as return itself) -- otherwise
        # injected capital would inflate that month's %, since NAV growth
        # would include money that was never actually invested-and-grew.
        contribution = -cash if cash < 0 else 0.0
        if contribution:
            cash = 0.0
        total_contributed += contribution

        mark_value = 0.0
        final_book = {}
        for sym, shares in book.items():
            px = _raw_open(sym) if sym in month_end_carry else None
            final_book[sym] = {"shares": shares,
                               "mark_price": round(px, 2) if px else None,
                               "value": round(shares * px, 2) if px else None}
            if px:
                mark_value += shares * px
        alloc["final_book"] = final_book

        nav = cash + mark_value
        base = (nav_prev if nav_prev is not None else seed_capital) + contribution
        ret_pct = ((nav - base) / base * 100.0) if base else 0.0
        nav_prev = nav
        alloc["picks"] = list(picks)
        alloc["cash_end"] = round(cash, 2)
        alloc["contribution"] = round(contribution, 2)

        monthly.append({"month": f"{y}-{m:02d}", "nav": round(nav, 2),
                        "cash": round(cash, 2), "n_held": len(book),
                        "slot_target": round(slot_target, 2),
                        "min_balance": round(slot_target * SLOTS_N),
                        "n_buys": len(buys), "days_to_fill": days_to_fill,
                        "return_pct_month": round(ret_pct, 3),
                        "contribution": round(contribution, 2),
                        "stop_pct": stop_pct,
                        "n_exits": len(result.exits), "allocations": alloc})

    final_state = {"book": book, "cash": cash, "seed_capital": seed_capital,
                   "carry_in": carry_in, "nav_prev": nav_prev,
                   "total_contributed": total_contributed}
    return monthly, seed_capital, final_state


CYCLES = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
         (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2),
         (2026, 3), (2026, 4), (2026, 5), (2026, 6)]

if __name__ == "__main__":
    print("Running PURE IN-AND-OUT...")
    pure, pure_seed = run_scenario(CYCLES, carry_forward_scenario=False)
    print("Running CARRY-FORWARD...")
    carry, carry_seed = run_scenario(CYCLES, carry_forward_scenario=True)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as fh:
        json.dump({"pure_in_and_out": pure, "pure_seed": pure_seed,
                   "carry_forward": carry, "carry_seed": carry_seed}, fh, indent=2)

    print(f"\n{'Month':<10}{'Pure NAV':>14}{'Pure mth%':>12}{'Carry NAV':>14}{'Carry mth%':>12}")
    for p, c in zip(pure, carry):
        print(f"{p['month']:<10}{p['nav']:>14,.0f}{p['return_pct_month']:>12.2f}"
             f"{c['nav']:>14,.0f}{c['return_pct_month']:>12.2f}")

    pure_total = (pure[-1]["nav"] - pure_seed) / pure_seed * 100.0
    carry_total = (carry[-1]["nav"] - carry_seed) / carry_seed * 100.0
    additive_pure = sum(p["return_pct_month"] for p in pure)
    additive_carry = sum(c["return_pct_month"] for c in carry)
    print(f"\nPure in-and-out: seed Rs {pure_seed:,.0f} -> final Rs {pure[-1]['nav']:,.0f} "
         f"({pure_total:+.2f}% compounded, {additive_pure:+.2f}% additive sum)")
    print(f"Carry-forward:   seed Rs {carry_seed:,.0f} -> final Rs {carry[-1]['nav']:,.0f} "
         f"({carry_total:+.2f}% compounded, {additive_carry:+.2f}% additive sum)")
