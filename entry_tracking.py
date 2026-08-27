"""
Multi-day entry-tracking note for NEW positions -- new-investor entries and
existing-investor buys into freshly emptied slots -- run after an expiry.

WHY THIS EXISTS
----------------
A single evening's quote does not get the whole basket filled: a real
16-cycle backtest found 38.8% of names miss their Day-1 limit, and every
miss was gap risk (opened past the limit), never an intraday-range miss.
This module runs the agreed 3-stage mechanism live (BACKTEST_LOG.md
section 11 -- restored 14-Aug-2026 after an unrequested 2-stage collapse
the same evening was reverted; the 2-stage version was never actually
agreed, see section 12's superseding note):

  Day 1: standard 20-day volatility-band LIMIT quote, whole-share sizing
         solved against the priciest stock's own band low x N slots. If
         the day's low never reaches the limit, the name misses.
  Day 2 (Day-1 misses only): the 20-day band is abandoned -- it
         demonstrably failed. A NEW limit, re-priced off Day-1's OWN
         realized volatility (80%-probability opening price, Parkinson
         estimator off that single day's H/L, anchored off Day-1's
         close). Shares resolved fresh against it. Still a real limit --
         fills if Day-2's low reaches it, misses if not.
  Day 3 (Day-2 misses only): pools Day-1 AND Day-2's realized vols for a
         steadier estimate, computes an 80%-probability opening price,
         and decides share count THE EVENING BEFORE (Day-2 close, no
         lookahead at Day-3's real price). Executes at Day-3's actual
         market open, no limit -- the basket must be complete.

RISK ANCHOR
-----------
Simple rule (26-Aug-2026, refined twice the same day): stop and target
are always measured off the MIN-BASKET PRICE that stage's share count
was actually sized against -- Day 1's quoted band limit, Day 2's
Parkinson re-quote, or Day 3's pooled-vol indicative estimate -- never
off wherever the fill actually executed, even when that differs (a
same-day gap through the limit, or Day 3's mandatory open). One number
governs both how many shares get bought and where the risk sits; the
actual execution price only matters for the P&L, never for the risk
band.

The one place this does NOT apply: the gap-risk abort check before
attempting Day 2 or Day 3 (below) deliberately stays pegged to Day-1's
actual open throughout, regardless of which stage's quote governs a
FILL -- that check asks "has this already broken the level we accepted
when we first decided to enter this month", which is a Day-1-anchored
question by definition, not a per-stage one.

History: this used to anchor EVERY fill to Day-1's open regardless of
which day or price it actually filled at, on the theory (Perold, 1988,
Implementation Shortfall) that a DELAYED fill must not inherit a stop
dragged toward a market that moved while waiting -- real evidence,
PNBHOUSING and BSE both got stopped from delayed, higher fills. Applying
that same rule to a same-day fill was never evidenced and broke the one
promise the message makes: ADANIGREEN's real 28-Jul cycle quoted and
filled Day 1 at 1367.44 against a 1383.10 open; anchoring to the open
gave a stop of 1313.945 (-3.91% off the real entry, not -5%) and
triggered a session early. First fix re-anchored to the actual fill
price; second fix (this one) re-anchors to the QUOTED min-basket price
instead, since that is the number the share count was sized against --
the two coincide whenever a limit fills without gapping through it (the
ADANIGREEN case), and only diverge when the open gaps below the quote.

GAP-RISK ABORT
--------------
Before attempting Day 2 or Day 3, or executing the Day-3 forced buy, the
PRECEDING day's low (or Day 3's own open) is checked against the
anchor-based stop. If price has already gapped through it, the entry is
ABORTED -- the slot stays in cash for the month, no fallback fill, never
backfilled from a lower-ranked name. Never buy something that's already
through its own risk boundary before you own it.

MESSAGE CADENCE (3-stage, restored 14-Aug-2026; "+1 repeat" retired 15-Aug-2026)
---------------------------------------------------------------------------------
  Day 0  (expiry evening, sent directly from cmd_sheet): Day-1 limit
         quotes, flat SL/exit % (the anchor isn't known yet).
  Day 1 eve (n=1): confirmed Day-1 fills (rupee SL/exit, anchored to
         Day-1's actual open) + the Day-2 LIMIT plan (re-priced, still a
         real order to place) for anything still open + dropped names +
         running basket size.
  Day 2 eve (n=2): confirmed Day-1 and Day-2 fills + the Day-3 MANDATORY
         market-buy plan (indicative price, locked share count, no order
         to place) for anything still open. IF every slot happens to be
         filled or dropped already at this point, this evening's message
         is the FINAL one -- Stream 2 (cycle_state's normal daily note)
         takes over starting the very next session.
  Day 3 eve (n=3), only reached if something was still open after Day 2:
         the Day-3 mandatory buy has now executed -- every slot is filled
         or dropped. This is the final message; Stream 2 takes over
         starting the next session.
  There is no extra "+1 confirmation" repeat any more (15-Aug-2026 fix --
  the evening resolution happens IS already the final "every slot is
  filled or dropped" message; repeating it unchanged one more evening
  was pure noise, e.g. when everything filled by Day 2 and Day 3 would
  otherwise have just echoed Day 2 verbatim). cmd_daily marks the window
  final_sent the same evening resolved_as_of gets set, so Stream 2 always
  resumes the NEXT session after resolution, whichever day that lands on.
Every day's message shows the FULL current state of every slot -- no
differential "what's new" logic, no separate reminder section (redundant
since SL/exit is already shown per stock).

PERSISTENCE
------------
Follows cycle_state.py's pattern: one atomic-write JSON state file,
advanced by exactly one session per call, so a job that runs late or was
skipped never loses track of where the chain is. A ledger record (kind
"entry_tracking") is ALSO written on every render -- the state file is
for "what to do next", the ledger is the permanent, never-rewritten
record of what was actually told to the investor that evening.
"""

import json
import logging
import math
import os
from datetime import date

import book
import config
import cycle_state
import daily_report
import ledger
import strategy

logger = logging.getLogger("momentum_tracker.entry_tracking")

STATE_FILE = os.path.join(config.DATA_DIR, "entry_tracking.json")
Z80 = 0.8416212  # 80th percentile, standard normal

ABORT_REASONS = {
    "before_day2": "price already broke the stop level on Day 1",
    "before_day3": "price already broke the stop level on Day 2",
    "day3_open_itself": "opened through the stop level on Day 3",
    "no_vol_data": "no usable price data to plan the next buy",
    "unresolved": "never resolved -- no price data available",
}


def _parkinson_sigma(row) -> float:
    h = float(row.get("high_price", 0) or 0)
    l = float(row.get("low_price", 0) or 0)
    if h <= 0 or l <= 0 or h < l:
        return None
    if h == l:
        return 0.0
    return math.log(h / l) / (2.0 * math.sqrt(math.log(2)))


def _vol_requote(close: float, sigma: float) -> float:
    """
    The Day-2/Day-3 80%-probability re-quote formula, factored out (26-Aug-
    2026) so advance() and reconcile() below call the exact same function
    instead of two copies that could quietly drift apart -- the same class
    of mistake as the KPITTECH tie-break bug, just one level removed: that
    bug was one function whose already-frozen OUTPUT went stale after a
    fix; a hand-duplicated formula here would be two IMPLEMENTATIONS that
    could disagree even on fresh input. One formula, one place.
    """
    return round(close * math.exp(Z80 * sigma), 2)


def load(path: str = None) -> dict:
    """
    entry_tracking.json holds TEMPORAL state only -- the day-by-day limit-
    chase machinery for a NOT-YET-settled order (which stage, what's
    currently quoted, has it breached the abort level). It never
    duplicates a fact book.json also stores: a pending order has no row
    in book.json at all until it fills, so there's nothing here for a
    code fix to leave stale relative to a second copy. (26-Aug-2026: a
    same-file merge with book.json was attempted and reverted -- it
    solved a problem this split never had, and introduced two real
    correctness bugs in the process; see git history / conversation log
    if reviving that idea.) book.json remains the sole place a FILLED
    position's price/shares live -- open_position()/close_position() are
    the only writers of those fields, from here.
    """
    path = path or STATE_FILE
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(state: dict, path: str = None) -> None:
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# expiry evening -- open the window
# ---------------------------------------------------------------------------

def open_window(expiry: date, symbols: list, stop_pct: float,
                target_pct=None, path: str = None,
                slot_target: float = None, min_portfolio: float = None,
                market_buy_symbols=None) -> dict:
    """
    Quote Day-1 limit prices for `symbols` (all 10 for a new investor, or
    just the empty slots for an existing one) off the expiry-day close.
    Nothing has traded yet -- risk_anchor is unknown until Day 1 closes.

    `market_buy_symbols` (15-Aug-2026 addendum, new-investor Day-0 fix):
    the subset of `symbols` that are CONTINUING names -- tagged HOLD in
    daily_report.build_entry_sheet's rows, i.e. the strategy didn't sell
    them at this expiry. The strategy's own accounting reprices a
    continuing name at Day-1's actual MARKET open, never a chased limit
    (the same reason _compute_hold_rebalance's TOP-UP executes at market
    with no limit chase) -- so for a new investor's entry price on these
    names to sit on the same basis as an existing investor's, they must
    also be bought at Day-1's market open, not quoted a Day-0 limit.
    These names skip the whole 3-stage limit chain entirely: they always
    fill on Day 1, unconditionally, at whatever the open turns out to be.

    `target_pct` accepts EITHER a single float (applied to every symbol,
    the pre-14-Aug-2026 behaviour) OR a {symbol: pct} dict -- needed
    because daily_report.build_entry_sheet resolves target_pct PER
    SYMBOL when config.LLM_TARGET_ENABLED (each row's own "Book at +X%"
    figure). Before this, cmd_sheet passed nothing at all, so every
    Day-1/2 "Exit: Rs Y" note silently used the flat config default even
    for a symbol the Day-0 sheet had just quoted a different LLM target
    for -- two conflicting instructions for the same position. A missing
    symbol in the dict, or a bare float, falls back to
    config.V4_TARGET_PCT, same as before.

    `slot_target`/`min_portfolio`, when given, come from
    daily_report.build_entry_sheet's sizing -- computed across the FULL
    kept basket (holds + buys), not just the buys passed here. Without
    this, an existing investor's BUY-only call would size against the
    priciest name among just the BUYS, which can differ from the
    priciest name in the whole 10-slot basket (e.g. if the priciest name
    is a HOLD, not a buy) -- silently sizing new positions to a different
    basket total than the one the HOLD rebalance step targets. Falls back
    to computing its own (buys-only) sizing when not given -- the
    brand-new-investor case, where every slot is a buy so there is no
    discrepancy to have.

    Loads its own price history rather than taking it as a parameter --
    keeps the call site in cmd_sheet a one-liner and matches how
    build_entry_sheet already sources its own data.
    """
    def _target_for(sym):
        if isinstance(target_pct, dict):
            return target_pct.get(sym, config.V4_TARGET_PCT)
        return config.V4_TARGET_PCT if target_pct is None else target_pct

    # A single representative value for the window-level display fallback
    # (Day-0's flat header when every symbol happens to share one target).
    flat_target_pct = (config.V4_TARGET_PCT if target_pct is None
                       else (target_pct if not isinstance(target_pct, dict)
                             else config.V4_TARGET_PCT))
    if not symbols:
        state = {"expiry": str(expiry), "slot_target": 0, "min_portfolio": 0,
                 "stop_pct": stop_pct, "target_pct": flat_target_pct, "sessions": [],
                 "resolved_as_of": str(expiry), "final_sent": False, "stocks": {}}
        save(state, path)
        return state

    hist = strategy.load_price_history(expiry, strategy.load_fo_universe())
    hist_dates = sorted(hist.keys())
    sig_frame = hist.get(expiry) if expiry in hist else hist.get(hist_dates[-1])

    rows = []
    for sym in symbols:
        if sig_frame is None or sym not in sig_frame.index:
            continue
        close = float(sig_frame.loc[sym, "close_price"])
        if close <= 0:
            continue
        lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
        rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

    if slot_target is None:
        sizing = daily_report._compute_min_portfolio_sizing(rows)
        slot_target = sizing["min_portfolio"] / max(1, len(rows))
        min_portfolio = sizing["min_portfolio"]
    else:
        # Re-solve whole-share counts against the SUPPLIED slot_target so
        # buy sizing agrees with the full-basket target, not a narrower
        # buys-only one.
        sizing = daily_report._resolve_shares_to_target(rows, slot_target)

    market_buy_symbols = set(market_buy_symbols or ())

    stocks = {}
    for sym in symbols:
        s = sizing["shares"].get(sym)
        if s is None:
            stocks[sym] = {"status": "no_data", "filled_day": None}
            continue
        stocks[sym] = {
            "status": "pending",
            "mandatory": False,
            "market_buy": sym in market_buy_symbols,
            "filled_day": None,
            # For a market_buy name this is an ESTIMATE only (Day-0 close
            # based), never a limit to place -- advance() ignores it for
            # the fill check and fills at whatever Day-1's real open is.
            "quote_price": s["limit_price"],
            "shares": s["shares"],
            "risk_anchor": None,
            "sl_price": None,
            "exit_price": None,
            "sigma1": None,
            "target_pct": _target_for(sym),
        }

    state = {
        "expiry": str(expiry),
        "slot_target": slot_target,
        "min_portfolio": sizing["min_portfolio"],
        "stop_pct": stop_pct,
        "target_pct": flat_target_pct,
        "sessions": [],
        "resolved_as_of": None,
        "final_sent": False,
        "stocks": stocks,
    }
    save(state, path)
    # 25-Aug-2026: give the book a complete row for every name in tonight's
    # basket immediately, not just whichever ones happen to already be a
    # real HOLD -- see book.seed_pending's docstring.
    book.seed_pending(symbols, expiry)
    logger.info("Opened entry-tracking window for %s: %d names, slot Rs %.0f",
               expiry, len(stocks), slot_target)
    return state


# ---------------------------------------------------------------------------
# one session
# ---------------------------------------------------------------------------

def advance(state: dict, day: date, path: str = None) -> dict:
    """
    Apply one session's real bhavcopy. Call once per trading day, in
    order, starting with the first session after the expiry that opened
    this window. Idempotent-ish: re-calling with the same `day` twice is
    harmless as long as it's the same `day` each time (checked below).

    n = len(state["sessions"]) after this call: 1 = Day 1 (vol-band limit
    day), 2 = Day 2 (Parkinson-repriced limit day, still a real order),
    3 = Day 3 (mandatory market day -- basket is complete after this),
    4+ = pure repeats / a safety net for any residual data gap.
    """
    if state["sessions"] and state["sessions"][-1] == str(day):
        return state  # already applied

    frame = cycle_state.frame_for(day)
    if frame is None:
        logger.warning("No bhavcopy for %s; entry-tracking session skipped", day)
        return state

    state["sessions"].append(str(day))
    n = len(state["sessions"])
    stop_pct = state["stop_pct"]
    slot_target = state["slot_target"]

    for sym, d in state["stocks"].items():
        if d["status"] in ("filled", "aborted", "no_data"):
            continue
        if sym not in frame.index:
            continue  # stale / suspended this session; try again next session
        row = frame.loc[sym]
        low = float(row.get("low_price", 0) or 0)
        opn = float(row.get("open_price", 0) or 0)
        close = float(row.get("close_price", 0) or 0)

        hist = d.setdefault("history", {})

        if n == 1:
            if opn <= 0:
                # No usable open today -- can't establish an anchor. Leave
                # pending and retry on the next session rather than risk a
                # Rs 0 anchor propagating into SL/exit downstream.
                logger.warning("No valid open for %s on %s; retrying next session", sym, day)
                continue
            # Provisional anchor, off today's open. This is the correct
            # basis for anything that DOESN'T fill today (the Day-2/3
            # gap-abort check just below needs a number now, before any
            # fill is known) -- but a name that fills TODAY re-anchors to
            # its own fill price a few lines down (26-Aug-2026 fix: see
            # that branch's comment for why opn was wrong for a same-day
            # limit fill).
            d["risk_anchor"] = opn
            anchor_stop = opn * (1 - stop_pct / 100.0)
            d["sl_price"] = round(anchor_stop, 2)
            # This symbol's OWN target, not the window-level flat value --
            # see open_window's docstring (14-Aug-2026 fix). Old windows
            # saved before this fix have no per-stock "target_pct", so fall
            # back to the window's flat value for those.
            sym_target_pct = d.get("target_pct", state["target_pct"])
            d["exit_price"] = round(opn * (1 + sym_target_pct / 100.0), 2)

            # Continuing (HOLD-tagged) name, new-investor market_buy path
            # (15-Aug-2026 addendum): no limit chase, no gap-risk-abort --
            # same reasoning as an existing investor's TOP-UP execution.
            # Always fills on Day 1, unconditionally, at the real open.
            if d.get("market_buy"):
                hist["day1"] = {"date": str(day), "proposed_price": "market_open",
                                "filled": True, "fill_price": round(opn, 2)}
                d.update(status="filled", filled_day=1, price=round(opn, 2))
                # Archive the OUTGOING record before rebasing it (26-Aug-
                # 2026 fix): book.open_position() below unconditionally
                # overwrites book[sym], and until this fix nothing ever
                # archived what was there first. A continuing HOLD's real
                # cost basis/entry_date/fill_history from the PRIOR cycle
                # was silently destroyed every month this ran -- caught
                # via TRENT/BANDHANBNK/POWERINDIA/GVT&D's real 29-Jul
                # entries vanishing with zero trace in book_archive.jsonl
                # when they were marked to the 26-Aug open. This is a
                # REBASE, not a sale -- economically a non-event, same
                # mark-to-market the carry-forward design always intended
                # -- so it archives at the SAME price/date the new record
                # opens at, purely to preserve the outgoing holding period
                # for later questions like "what did this cycle actually
                # hold" (book.holdings_for_expiry).
                prior = book.get(sym)
                if prior is not None and prior.get("entry_price") is not None:
                    book.close_position(sym, exit_price=opn, exit_date=day,
                                        reason="REBASE")
                book.open_position(sym, d["shares"], opn, day, state["expiry"],
                                   risk_anchor=opn, fill_history=hist,
                                   target_pct=d.get("target_pct", state["target_pct"]),
                                   stop_pct=state["stop_pct"])
                continue

            # Snapshot BEFORE any mutation below -- this is the Day-1
            # 20-day-band limit actually quoted on Day 0, which a miss
            # overwrites in place with the Day-2 re-quote a few lines down.
            day1_quote = d["quote_price"]

            # Fill check FIRST, always -- the abort threshold sits below
            # the quoted price in effectively every real case, so a
            # session that dipped to the abort level has already passed
            # through the fill level on the way down. Checking abort
            # first would wrongly abandon something that actually filled.
            if low > 0 and low <= d["quote_price"]:
                fill_price = min(opn, d["quote_price"])
                # Re-anchor to the ACTUAL fill price, not the open (26-Aug-
                # 2026 fix). The open-anchor rule exists to stop a DELAYED
                # (Day-2/3) fill from dragging the stop toward a market
                # that has moved in the meantime -- there is no such delay
                # here, the fill happened today, at this price, full stop.
                # Anchoring to the open anyway means a limit that fills
                # BELOW the open (a genuinely good entry -- the whole
                # point of quoting a limit under the open at all) gets a
                # stop measured from a price never paid: found on
                # ADANIGREEN's real 28-Jul cycle, quoted/filled 1367.44
                # against a 1383.10 open, anchor-off-open put the stop at
                # 1313.945 (-3.91% from the real entry, not -5%) and
                # triggered it a full session earlier than an entry-based
                # 5% stop (1299.07) would have. "SL: -5%" in the message
                # must mean 5% off what was actually paid.
                # Anchor to the QUOTED min-basket price, not the fill price
                # (26-Aug-2026 refinement, same day as the fix above): the
                # share count was sized against day1_quote, so the risk
                # band should be measured off the same number the sizing
                # was -- one price governs both, simple and consistent.
                # This only differs from fill_price when the open itself
                # gapped BELOW the quote (a better-than-planned entry);
                # ADANIGREEN filled AT its quote with no gap, so this
                # matches the fix above exactly for that real case.
                d["risk_anchor"] = day1_quote
                anchor_stop = day1_quote * (1 - stop_pct / 100.0)
                d["sl_price"] = round(anchor_stop, 2)
                sym_target_pct = d.get("target_pct", state["target_pct"])
                d["exit_price"] = round(day1_quote * (1 + sym_target_pct / 100.0), 2)
                hist["day1"] = {"date": str(day), "proposed_price": day1_quote,
                                "filled": True, "fill_price": round(fill_price, 2)}
                d.update(status="filled", filled_day=1, price=round(fill_price, 2))
                book.open_position(sym, d["shares"], fill_price, day, state["expiry"],
                                   risk_anchor=day1_quote, fill_history=hist,
                                   target_pct=sym_target_pct, stop_pct=state["stop_pct"])
                continue
            hist["day1"] = {"date": str(day), "proposed_price": day1_quote, "filled": False}
            if low > 0 and low <= anchor_stop:
                d.update(status="aborted", aborted_stage="before_day2")
                continue
            # Missed Day 1: the 20-day band demonstrably failed, so Day 2
            # gets a NEW limit, re-priced off Day-1's own realized
            # volatility (80%-probability opening estimate, Parkinson
            # off today's H/L). Still a real order to place -- NOT yet
            # mandatory.
            sigma1 = _parkinson_sigma(row)
            d["sigma1"] = sigma1
            if sigma1 is None or close <= 0:
                # No usable range for THIS name today -- fall back to the
                # standard 20-day band's upper edge as the re-quote price
                # rather than blocking on one name.
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day: frame}, close or opn)
                d["quote_price"] = round(hi, 2)
            else:
                d["quote_price"] = _vol_requote(close, sigma1)
            d["shares"] = max(1, round(slot_target / d["quote_price"]))
            # Preview refresh (26-Aug-2026 fix): sl_price/exit_price above
            # were computed off `opn` (today's open) as a provisional
            # stand-in before any real quote existed -- correct for the
            # gap-abort check, but stale as an investor-facing preview
            # now that Day-2's real re-quote is known. A message rendered
            # tonight would otherwise show a "Limit buy" at the NEW quote
            # next to an SL/Exit still measured off today's open -- caught
            # via KALYANKJIL/NAM-INDIA's real 26-Aug pending rows, where
            # both diverged from a flat 5%/40% off the quote. Same
            # "whichever price the fill would anchor to" rule the FILL
            # branches already use; risk_anchor itself is untouched here,
            # it must stay the Day-1 open for the abort-check above.
            sym_target_pct = d.get("target_pct", state["target_pct"])
            d["sl_price"] = round(d["quote_price"] * (1 - stop_pct / 100.0), 2)
            d["exit_price"] = round(d["quote_price"] * (1 + sym_target_pct / 100.0), 2)

        elif n == 2:
            if opn <= 0:
                logger.warning("No valid open for %s on %s; retrying next session", sym, day)
                continue
            anchor = d["risk_anchor"]
            anchor_stop = anchor * (1 - stop_pct / 100.0)
            day2_quote = d["quote_price"]
            # Fill check first, same reasoning as Day 1.
            if low > 0 and low <= d["quote_price"]:
                fill_price = min(opn, d["quote_price"])
                # Same re-anchor as Day 1: this fill is not delayed past
                # its own decision day, so it anchors to day2_quote (what
                # its own share count was sized against), not Day-1's
                # open. `anchor` (Day-1's open) stays the ABORT-CHECK
                # threshold above -- that one genuinely needs to ask "has
                # this already broken through the ORIGINAL Day-1 level"
                # -- but the risk_anchor actually recorded on a fill is
                # the fill's own stage.
                d["risk_anchor"] = day2_quote
                anchor_stop_out = day2_quote * (1 - stop_pct / 100.0)
                d["sl_price"] = round(anchor_stop_out, 2)
                sym_target_pct = d.get("target_pct", state["target_pct"])
                d["exit_price"] = round(day2_quote * (1 + sym_target_pct / 100.0), 2)
                hist["day2"] = {"date": str(day), "proposed_price": day2_quote,
                                "filled": True, "fill_price": round(fill_price, 2)}
                d.update(status="filled", filled_day=2, price=round(fill_price, 2))
                book.open_position(sym, d["shares"], fill_price, day, state["expiry"],
                                   risk_anchor=day2_quote, fill_history=hist,
                                   target_pct=sym_target_pct, stop_pct=state["stop_pct"])
                continue
            hist["day2"] = {"date": str(day), "proposed_price": day2_quote, "filled": False}
            if low > 0 and low <= anchor_stop:
                d.update(status="aborted", aborted_stage="before_day3")
                continue
            # Missed Day 2 too: Day 3 is now the MANDATORY market buy --
            # pools Day-1 AND Day-2's realized vols for a steadier
            # estimate, and decides share count THE EVENING BEFORE (no
            # lookahead at Day-3's real price). This price is indicative
            # only, used to lock the share count tonight -- Day 3 does
            # not have a limit to place.
            d["mandatory"] = True
            sigma1 = d.get("sigma1")
            sigma2 = _parkinson_sigma(row)
            d["sigma2"] = sigma2
            sigmas = [s for s in (sigma1, sigma2) if s is not None]
            pooled_sigma = sum(sigmas) / len(sigmas) if sigmas else None
            if pooled_sigma is None or close <= 0:
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day: frame}, close or opn)
                d["quote_price"] = round(hi, 2)
            else:
                d["quote_price"] = _vol_requote(close, pooled_sigma)
            d["shares"] = max(1, round(slot_target / d["quote_price"]))
            # Same preview refresh as the Day-1-miss branch above -- the
            # Day-3 quote just changed, so the preview must move with it.
            # `anchor` (risk_anchor, Day-1's open) is untouched, still the
            # abort-check basis.
            sym_target_pct = d.get("target_pct", state["target_pct"])
            d["sl_price"] = round(d["quote_price"] * (1 - stop_pct / 100.0), 2)
            d["exit_price"] = round(d["quote_price"] * (1 + sym_target_pct / 100.0), 2)

        elif n == 3:
            anchor = d["risk_anchor"]
            anchor_stop = anchor * (1 - stop_pct / 100.0)
            day3_quote = d["quote_price"]
            if opn > 0 and opn <= anchor_stop:
                hist["day3"] = {"date": str(day), "proposed_price": day3_quote, "filled": False}
                d.update(status="aborted", aborted_stage="day3_open_itself")
                continue
            if opn <= 0:
                # No usable open -- can't force-fill responsibly today,
                # try again next session (falls through to the n>=4 net
                # below if it never resolves).
                logger.warning("No valid open for %s on %s; Day-3 buy retried next session", sym, day)
                continue
            # Mandatory: fills at the actual open, no limit, using the
            # share count already locked in on Day 2's evening. Anchors to
            # day3_quote -- the SAME indicative price the share count was
            # sized against the evening before -- not the actual open it
            # executes at, matching Day 1/2's re-anchor above: one number
            # governs both sizing and risk, and the mandatory buy's price
            # is not known until the moment it fires, so the plan made the
            # evening before is the only number to be consistent with.
            anchor_stop_out = day3_quote * (1 - stop_pct / 100.0)
            d["sl_price"] = round(anchor_stop_out, 2)
            sym_target_pct = d.get("target_pct", state["target_pct"])
            d["exit_price"] = round(day3_quote * (1 + sym_target_pct / 100.0), 2)
            hist["day3"] = {"date": str(day), "proposed_price": day3_quote,
                            "filled": True, "fill_price": round(opn, 2)}
            d.update(status="filled", filled_day=3, price=round(opn, 2))
            book.open_position(sym, d["shares"], opn, day, state["expiry"],
                               risk_anchor=day3_quote, fill_history=hist,
                               target_pct=sym_target_pct, stop_pct=state["stop_pct"])

        else:
            # Day 4+: nothing should still be open at this point (Day 3 is
            # unconditional). Anything still pending here is a genuine
            # data gap that never resolved -- abort rather than hang the
            # window open indefinitely.
            d.update(status="aborted", aborted_stage="unresolved")

    if state["resolved_as_of"] is None:
        if all(d["status"] in ("filled", "aborted", "no_data")
               for d in state["stocks"].values()):
            state["resolved_as_of"] = str(day)

    save(state, path)
    return state


def reconcile(state: dict = None, path: str = None) -> list:
    """
    Recomputes today's quote, fresh, for every still-pending symbol in the
    given (or loaded) window using CURRENT code, and diffs it against the
    frozen quote_price actually sitting in state. Returns a list of
    mismatches -- empty means every frozen quote still matches what the
    code would produce if asked again right now.

    THE GAP THIS CLOSES (26-Aug-2026): open_window()/advance() compute a
    quote ONCE and freeze it for the life of that stage. If daily_report.
    py's pricing logic (or this module's own _vol_requote/_parkinson_
    sigma) changes while a window is already open, nothing previously
    noticed that an already-quoted, still-pending price no longer matches
    what the code would say if asked again -- which is exactly how
    KPITTECH/NATIONALUM ended up marked FILLED against a quote a same-day
    tie-break fix had already superseded. Call this before any message
    sends, or any time daily_report.py's pricing functions change, to get
    a loud, explicit mismatch instead of a silent stale one.

    Deliberately reuses the SAME functions open_window()/advance() call
    (_compute_stock_entry_band, _solve_shares_to_slot for a Day-1 quote;
    _vol_requote for a Day-2/3 re-quote) rather than a second, hand-
    written formula -- a duplicate implementation here would just be this
    exact bug class one level up, the mistake already made and reverted
    once tonight in a different form.
    """
    if state is None:
        state = load(path)
    if state is None:
        return []

    mismatches = []
    sessions = state.get("sessions", [])
    n = len(sessions)

    for sym, d in state["stocks"].items():
        if d.get("status") != "pending" or d.get("market_buy"):
            continue  # market_buy names never carry a real limit to check

        frozen = d.get("quote_price")
        if frozen is None:
            continue

        fresh_price = None
        if n == 0:
            # Still on the original Day-0/Day-1 band quote -- recompute
            # exactly like open_window() did.
            expiry_dt = date.fromisoformat(state["expiry"])
            hist = strategy.load_price_history(expiry_dt, strategy.load_fo_universe())
            hist_dates = sorted(hist.keys())
            sig_frame = hist.get(expiry_dt) if expiry_dt in hist else hist.get(hist_dates[-1])
            if sig_frame is None or sym not in sig_frame.index:
                continue
            close = float(sig_frame.loc[sym, "close_price"])
            if close <= 0:
                continue
            lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
            _n_shares, fresh_price, _dev = daily_report._solve_shares_to_slot(
                state["slot_target"], {"close": close, "entry_lo": lo, "entry_hi": hi})
        elif not d.get("mandatory"):
            # Day-2 re-quote: re-derive from Day-1's own realized H/L --
            # historical fact by now, it cannot itself change.
            day1 = date.fromisoformat(sessions[0])
            frame = cycle_state.frame_for(day1)
            if frame is None or sym not in frame.index:
                continue
            row = frame.loc[sym]
            close = float(row.get("close_price", 0) or 0)
            sigma1 = _parkinson_sigma(row)
            if sigma1 is None or close <= 0:
                opn = float(row.get("open_price", 0) or 0)
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day1: frame}, close or opn)
                fresh_price = round(hi, 2)
            else:
                fresh_price = _vol_requote(close, sigma1)
        else:
            # Day-3 pooled re-quote: re-derive from Day-1 AND Day-2's
            # realized H/L.
            if len(sessions) < 2:
                continue
            day1, day2 = date.fromisoformat(sessions[0]), date.fromisoformat(sessions[1])
            frame1, frame2 = cycle_state.frame_for(day1), cycle_state.frame_for(day2)
            if frame2 is None or sym not in frame2.index:
                continue
            row2 = frame2.loc[sym]
            close = float(row2.get("close_price", 0) or 0)
            sigma1 = (_parkinson_sigma(frame1.loc[sym])
                     if frame1 is not None and sym in frame1.index else None)
            sigma2 = _parkinson_sigma(row2)
            sigmas = [s for s in (sigma1, sigma2) if s is not None]
            pooled = sum(sigmas) / len(sigmas) if sigmas else None
            if pooled is None or close <= 0:
                opn2 = float(row2.get("open_price", 0) or 0)
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day2: frame2}, close or opn2)
                fresh_price = round(hi, 2)
            else:
                fresh_price = _vol_requote(close, pooled)

        if fresh_price is not None and abs(fresh_price - frozen) > 0.01:
            mismatches.append({
                "symbol": sym,
                "frozen_quote": frozen,
                "current_code_quote": fresh_price,
                "delta": round(fresh_price - frozen, 2),
                "stage": "day1" if n == 0 else ("day3" if d.get("mandatory") else "day2"),
            })
    return mismatches


def apply_reconcile_fixes(mismatches: list, state: dict = None, path: str = None) -> dict:
    """
    Applies reconcile()'s own findings directly -- no manual transcription
    step between "the check computed X" and "the file gets X" (26-Aug-2026,
    explicit ask: hand-retyping a number the check already computed is
    exactly the failure mode this check exists to eliminate, and it's how
    the FIRST live use of reconcile() tonight was itself fixed -- by hand,
    from the printed output, which defeated half the point).

    For each mismatch, writes mismatch["current_code_quote"] as the new
    quote_price and recomputes shares/sl_price/exit_price from it using
    the exact same formulas advance() uses. Never touches fill_history/
    history (the permanent record of what was actually quoted/filled on a
    PAST day) -- only the live, still-open quote and what's derived from
    it. reconcile() already restricts its findings to status=="pending",
    non-market_buy symbols, so anything in `mismatches` is safe to touch
    this way by construction.

    Returns the updated state; does not decide anything on its own --
    still call reconcile() again afterward and look at the result before
    trusting it, same as any other change here tonight.
    """
    if state is None:
        state = load(path)
    if state is None or not mismatches:
        return state

    slot_target = state["slot_target"]
    stop_pct = state["stop_pct"]

    for m in mismatches:
        sym = m["symbol"]
        d = state["stocks"].get(sym)
        if d is None:
            continue
        new_quote = m["current_code_quote"]
        old_quote = d["quote_price"]
        d["quote_price"] = new_quote
        d["shares"] = max(1, round(slot_target / new_quote))
        sym_target_pct = d.get("target_pct", state["target_pct"])
        d["sl_price"] = round(new_quote * (1 - stop_pct / 100.0), 2)
        d["exit_price"] = round(new_quote * (1 + sym_target_pct / 100.0), 2)
        logger.warning("reconcile: %s quote %.2f -> %.2f (shares %d, sl %.2f, exit %.2f)",
                       sym, old_quote, new_quote, d["shares"], d["sl_price"], d["exit_price"])

    save(state, path)
    return state


def is_window_active(state: dict) -> bool:
    """
    True while an entry-tracking (Stream 1) message should still be sent
    instead of the normal daily note. True until every slot is filled or
    aborted; the SAME evening that resolves, cmd_daily marks
    final_sent right after sending that "every slot is filled or
    dropped" message, so this goes False starting the very next session
    (15-Aug-2026 fix -- no extra "+1" repeat evening any more).
    """
    if state is None:
        return False
    if state["resolved_as_of"] is None:
        return True
    return not state["final_sent"]


def mark_final_sent(state: dict, path: str = None) -> None:
    """
    Called the same evening the window resolves, right after the final
    "every slot filled or dropped" message has already been rendered and
    recorded (cmd_daily's order: advance -> render -> record -> this).

    Deletes entry_tracking's own state file rather than leaving it around
    flagged final_sent=True (26-Aug-2026 change, explicit ask: "flush it
    once its job is done else somebody might misread some data from it").
    Nothing unique is lost -- every fact worth keeping already transferred
    to book.json the moment each slot actually filled (see load()'s
    docstring), and ledger.jsonl/notes/*.txt already hold the permanent,
    append-only record of what was actually sent. A resolved-but-still-
    present entry_tracking.json is pure downside: it looks like live state
    to anything that doesn't specifically check final_sent first, right up
    until the next expiry's open_window() silently overwrites it anyway.
    Deleting it makes "no active window" the same thing as "no file",
    which is also exactly what load() already treats a missing file as.
    """
    path = path or STATE_FILE
    if os.path.exists(path):
        os.remove(path)
    logger.info("Entry-tracking window for %s resolved and sent -- state file cleared",
               state.get("expiry"))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _current_basket_estimate(state: dict) -> float:
    """
    Running basket size: confirmed rupees for filled slots, indicative
    rupees (shares x current quote) for anything still pending. Becomes
    exact once every slot is filled or dropped.
    """
    total = 0.0
    for d in state["stocks"].values():
        if d["status"] == "filled":
            total += d["shares"] * d["price"]
        elif d["status"] == "pending":
            total += d["shares"] * d["quote_price"]
    return total


def _cycle_title(state: dict, as_of: str) -> str:
    """
    "<StartMonth>-<EndMon> '<YY> PORTFOLIO - <date>" header used from Day 1
    onward (15-Aug-2026 addendum), e.g. "JULY-AUG '26 PORTFOLIO -
    30-Jul-2026". Day 0 keeps its own "Entry tracking -- <expiry> expiry"
    header (existing investors) / the separate new-investor Day-0 title --
    this is deliberately NOT used there.

    The end month is the calendar month after the expiry's month, not the
    real next expiry date (entry_tracking's state doesn't carry that) --
    fine for the normal case since a cycle runs expiry-to-expiry roughly
    one calendar month apart. A Dec-Jan rollover would need the label
    fixed by hand; not handled here.
    """
    import calendar
    import datetime as _dt

    expiry_dt = _dt.date.fromisoformat(state["expiry"])
    start_month = expiry_dt.strftime("%B").upper()
    end_month_num = expiry_dt.month % 12 + 1
    end_month = calendar.month_abbr[end_month_num].upper()
    yy = f"{expiry_dt.year % 100:02d}"
    date_str = _dt.date.fromisoformat(as_of).strftime("%d-%b-%Y")
    return f"<b>{start_month}-{end_month} '{yy} PORTFOLIO - {date_str}</b>"


def render(state: dict) -> str:
    from alerts import esc

    n = len(state["sessions"])
    if n == 0:
        L = [f"<b>Entry tracking — {state['expiry']} expiry</b>"]
    else:
        as_of = state["sessions"][-1]
        L = [_cycle_title(state, as_of)]

    if n == 0:
        # Day 0 (expiry evening): anchor unknown, percentage(s) only.
        L.append("<i>Basket from today's close — place limit orders at tomorrow's open</i>")
        L.append("")
        targets = {d["target_pct"] for d in state["stocks"].values()
                  if d.get("status") != "no_data" and "target_pct" in d}
        if len(targets) <= 1:
            # Every symbol shares one target (the common case, and the
            # only case pre-14-Aug-2026) -- one flat header, as before.
            L.append(f"<b>SL: -{state['stop_pct']:.0f}%  |  Exit: +{state['target_pct']:.0f}%</b> "
                     f"(applies to your actual fill price)")
        else:
            # LLM_TARGET_ENABLED gave symbols different targets -- a
            # single flat header would misstate some of them, so drop it
            # and show each symbol's own exit % on its own line instead.
            L.append(f"<b>SL: -{state['stop_pct']:.0f}%</b> for every name "
                     f"(applies to your actual fill price); exit target varies by name, see below")
        L.append("")
        for sym, d in state["stocks"].items():
            if d["status"] == "no_data":
                L.append(f"{esc(sym)} — no usable price data, skipped")
                continue
            suffix = (f"  Exit: +{d['target_pct']:.0f}%" if len(targets) > 1 else "")
            L.append(f"<b>{esc(sym)}</b>  Limit buy: {daily_report._fmt_money(d['quote_price'])}  "
                     f"Qty: {d['shares']:,}{suffix}")
        return "\n".join(L).strip()

    if state["resolved_as_of"] is not None:
        L.append("<i>Final fill list — every slot is filled or dropped</i>")
    L.append("")

    filled = [(s, d) for s, d in state["stocks"].items() if d["status"] == "filled"]
    # Two different kinds of "still open" now (3-stage, restored
    # 14-Aug-2026): a Day-2 re-quote is a REAL limit order to place
    # tomorrow (d["mandatory"] is False); a Day-3 plan is an indicative
    # price only, nothing to place, buys unconditionally at the open
    # (d["mandatory"] is True). Conflating them under one "MANDATORY"
    # header (the 2-stage version's bug) told the investor to do nothing
    # on a night they actually needed to place a new limit order.
    pending_limit = [(s, d) for s, d in state["stocks"].items()
                     if d["status"] == "pending" and not d.get("mandatory")]
    pending_mandatory = [(s, d) for s, d in state["stocks"].items()
                         if d["status"] == "pending" and d.get("mandatory")]
    aborted = [(s, d) for s, d in state["stocks"].items() if d["status"] == "aborted"]
    no_data = [(s, d) for s, d in state["stocks"].items() if d["status"] == "no_data"]

    # 26-Aug-2026: one line per stock instead of two -- explicit ask, the
    # two-line-per-stock layout read as a wall of text on Telegram.
    # 27-Aug-2026: numbered within each section -- matches
    # render_new_investor_day0's numbered list, dropped by accident when
    # this was rewritten from two lines to one. Restarts at 1 per section
    # (FILLED / LIMIT BUY / MANDATORY), not one running count across all
    # three, since each is a distinct list an investor checks off
    # separately.
    if filled:
        L.append("<b>FILLED</b>")
        for i, (sym, d) in enumerate(filled, 1):
            L.append(f"{i}. <b>{esc(sym)}</b>: {d['shares']:,} qty @ "
                     f"{daily_report._fmt_money(d['price'])}  (Day {d['filled_day']})  "
                     f"SL: {daily_report._fmt_money(d['sl_price'])}  "
                     f"Exit: {daily_report._fmt_money(d['exit_price'])}")
        L.append("")

    if pending_limit:
        L.append("<b>LIMIT BUY — TOMORROW (re-priced, Day-1 limit missed)</b>")
        for i, (sym, d) in enumerate(pending_limit, 1):
            sl_exit = ""
            if d.get("sl_price") is not None:
                sl_exit = (f"  SL: {daily_report._fmt_money(d['sl_price'])}  "
                          f"Exit: {daily_report._fmt_money(d['exit_price'])}")
            L.append(f"{i}. <b>{esc(sym)}</b>: {d['shares']:,} qty @ "
                     f"{daily_report._fmt_money(d['quote_price'])}{sl_exit}")
        L.append("")

    if pending_mandatory:
        L.append("<b>MANDATORY MARKET BUY — TOMORROW</b>")
        for i, (sym, d) in enumerate(pending_mandatory, 1):
            sl_exit = ""
            if d.get("sl_price") is not None:
                sl_exit = (f"  SL: {daily_report._fmt_money(d['sl_price'])}  "
                          f"Exit: {daily_report._fmt_money(d['exit_price'])}")
            L.append(f"{i}. <b>{esc(sym)}</b>: {d['shares']:,} qty @ "
                     f"~{daily_report._fmt_money(d['quote_price'])} (market buy){sl_exit}")
        L.append("")

    if aborted or no_data:
        L.append("<b>DROPPED — do not enter, slot stays in cash this month</b>")
        for sym, d in aborted:
            reason = ABORT_REASONS.get(d.get("aborted_stage"), d.get("aborted_stage"))
            L.append(f"{esc(sym)} — {esc(reason)}")
        for sym, d in no_data:
            L.append(f"{esc(sym)} — no usable price data, skipped")
        L.append("")

    basket = _current_basket_estimate(state)
    still_pending = bool(pending_limit or pending_mandatory)
    label = "Final basket size" if not still_pending else "Basket size so far (indicative)"
    L.append(f"<b>{label}: {daily_report._fmt_money(basket)}</b>")
    L.append("")

    L.append("<i>Please check the entry price / stop-loss / exit price above "
             "and update your order book accordingly.</i>")
    return "\n".join(L).strip()


def render_new_investor_day0(state: dict) -> str:
    """
    New-investor variant of the Day-0 message (15-Aug-2026 addendum).
    Numbered-list format, explicit "minimum basket size" quoted from the
    full Day-0 sizing, "upscale as needed" note -- distinct copy from the
    general render() Day-0 output, which existing investors also see.

    A new investor owns nothing, so unlike render() (which can carry a
    mix of BUY and HOLD/TOP-UP rows for an existing investor's basket)
    every slot here is a fresh entry for this investor -- the caller is
    responsible for having opened the window with ALL basket symbols
    (see daily_report.build_entry_sheet's 'action' tag warning: HOLD
    there means "continuing from last cycle's strategy basket", not
    "already in this investor's book" -- do not filter on it here).

    Two of those slots still print differently (15-Aug-2026 fix): names
    tagged `market_buy` in the state (the HOLD/continuing ones, passed as
    `market_buy_symbols` to open_window) get a "buy at tomorrow's market
    open" instruction instead of a limit price -- matching the same
    entry-price basis an EXISTING investor gets for those same names
    (the strategy reprices a continuing position at Day-1's actual open,
    never a chased limit; see open_window's docstring). Everything else
    is a genuine fresh-buy limit quote, unchanged.

    Day-0 only for now (n == 0) -- Day-1+ new-investor copy is a separate,
    not-yet-built variant.
    """
    from alerts import esc
    import datetime as _dt

    n = len(state["sessions"])
    if n != 0:
        raise NotImplementedError(
            "render_new_investor_day0 only covers the Day-0 (n=0) message; "
            "Day-1+ new-investor copy is not built yet")

    expiry_dt = _dt.date.fromisoformat(state["expiry"])
    # NOT strftime("%-d ...") -- that no-leading-zero flag is a Linux/
    # glibc strftime extension. It works fine in dev (Linux sandbox) and
    # raises ValueError: Invalid format string on the real Windows
    # production machine, where this actually runs (25-Aug-2026: this is
    # exactly what silently killed tonight's expiry-evening run -- no
    # log line past "Opened entry-tracking window", no failure alert,
    # nothing sent, because this call sat outside cmd_sheet's try/except).
    header_date = f"{expiry_dt.day} {expiry_dt.strftime('%B %Y')}"

    targets = {d["target_pct"] for d in state["stocks"].values()
              if d.get("status") != "no_data" and "target_pct" in d}
    flat_targets = len(targets) <= 1

    L = [f"<b>Fresh Investment for NEW investors — {header_date}</b>",
         f"Please place all these buy orders immediately "
         f"(minimum basket size: ~INR {daily_report._fmt_money(state['min_portfolio'])}, "
         f"upscale as needed):",
         ""]
    if flat_targets:
        L.append(f"(SL: -{state['stop_pct']:.0f}%, Exit: +{state['target_pct']:.0f}%)")
    else:
        L.append(f"(SL: -{state['stop_pct']:.0f}% for every name; "
                 f"exit target varies by name, see below)")
    L.append("")

    # Limit-buy names first, market-open (continuing/HOLD-tagged) names
    # last -- 15-Aug-2026 reorder, keeps the "place these orders now" list
    # grouped by order TYPE so the two market-open lines don't interrupt
    # the run of limit prices.
    items = [(sym, d) for sym, d in state["stocks"].items() if d["status"] != "no_data"]
    limit_items = [(sym, d) for sym, d in items if not d.get("market_buy")]
    market_items = [(sym, d) for sym, d in items if d.get("market_buy")]

    i = 0
    for sym, d in limit_items + market_items:
        i += 1
        suffix = f" (Exit: +{d['target_pct']:.0f}%)" if not flat_targets else ""
        share_word = "share" if d["shares"] == 1 else "shares"
        if d.get("market_buy"):
            L.append(f"{i}. {esc(sym)}: {d['shares']:,} {share_word} "
                     f"@ market open{suffix}")
        else:
            L.append(f"{i}. {esc(sym)}: {d['shares']:,} {share_word} @ INR "
                     f"{daily_report._fmt_money(d['quote_price'])}{suffix}")

    return "\n".join(L).strip()


def record(state: dict, rendered: str) -> bool:
    return ledger.record_note(
        "entry_tracking", state["expiry"], rendered=rendered,
        session=len(state["sessions"]),
        resolved_as_of=state["resolved_as_of"],
        stocks={sym: {k: v for k, v in d.items()} for sym, d in state["stocks"].items()},
    )
