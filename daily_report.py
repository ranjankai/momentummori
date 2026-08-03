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
    to_buy: list = field(default_factory=list)
    to_sell: list = field(default_factory=list)
    sell_orders: list = field(default_factory=list)
    buy_orders: list = field(default_factory=list)
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

    hist = strategy.load_price_history(expiry, symbols)
    if expiry not in hist:
        raise strategy.StrategyError(f"No bhavcopy for governing expiry {expiry}")

    import nse_client
    import scoring
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(expiry))
    signals = strategy.compute_signals_cached(hist, fo, expiry, symbols)
    basket, full = strategy.rank_universe(signals, sectors)
    basket_symbols = basket["symbol"].tolist()

    # Apply the surveillance veto HERE, before the simulation, because
    # this is the basket that was actually bought. build_entry_sheet
    # vetoes and backfills; this function used to call apply_veto only
    # after the walk and throw the kept list away, so the evening note
    # tracked a portfolio nobody owned. On 03-Aug-2026 the report followed
    # KALYANKJIL (ASM Stage I, vetoed out) while the real book held
    # ADANIGREEN, and every figure in the note -- including cycle
    # performance -- was computed on the wrong ten names.
    ranked_order = list(full.index)
    veto_dropped, veto_ran = [], False
    if config.VETO_ENABLED:
        try:
            import surveillance
            kept, veto_dropped, _added, veto_ran = surveillance.apply_veto(
                basket, ranked_order, sectors, session)
            if veto_ran and kept:
                basket_symbols = kept
                # simulate_month fills its slots by walking ranked_order,
                # NOT basket_symbols -- the latter only decides carry-forward
                # HOLD vs SELL. So the vetoed names have to come out of the
                # ranking itself, otherwise the walk buys them anyway and a
                # mid-month replacement could pick one too.
                blocked = {s for s, _why in veto_dropped}
                ranked_order = kept + [s for s in ranked_order
                                       if s not in kept and s not in blocked]
        except Exception as exc:                      # never break the note
            logger.error("Veto step failed, continuing without it: %s", exc)
            veto_ran = False

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
    holdings, exits, to_buy, mtd = (res.open_positions, res.exits,
                                    res.to_buy, res.return_pct)

    rpt = Report(as_of=as_of, expiry=expiry, entry_date=days[0],
                 holdings=holdings, exits=exits, to_buy=to_buy,
                 mtd_return_pct=mtd,
                 empty_slots=config.PORTFOLIO_SIZE - len(holdings) - len(to_buy))

    # --- actionable orders -------------------------------------------------
    # A target sell is only accepted by the exchange once it is inside the
    # dynamic price band, so it is issued the evening it becomes placeable
    # rather than on entry day.
    for h in rpt.holdings:
        if h.target_placeable:
            rpt.sell_orders.append({
                "symbol": h.symbol, "kind": "TARGET",
                "limit": round(h.target, 2),
                "last": round(h.last, 2) if h.last else None,
            })
    for sym in rpt.to_sell:
        rpt.sell_orders.append({"symbol": sym, "kind": "MOMENTUM", "limit": None})

    band = config.ENTRY_BAND_PCT / 100.0
    stop = stop_pct / 100.0
    last_frame = merged.get(as_of)
    for sym in rpt.to_buy:
        ref = None
        if last_frame is not None and sym in last_frame.index:
            val = last_frame.at[sym, "close_price"]
            if pd.notna(val) and val > 0:
                ref = float(val)
        if ref is None:
            logger.warning("No reference close for pending buy %s; "
                           "quoting market only", sym)
            rpt.buy_orders.append({"symbol": sym, "lo": None, "hi": None,
                                   "sl_lo": None, "sl_hi": None})
            continue
        lo, hi = ref * (1 - band), ref * (1 + band)
        rpt.buy_orders.append({
            "symbol": sym,
            "lo": round(lo, 2), "hi": round(hi, 2),
            "sl_lo": round(lo * (1 - stop), 2),
            "sl_hi": round(hi * (1 - stop), 2),
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
            note = (f" (⚠ extrapolated from {perf['n_months']} month(s) — "
                    f"not a track record until 1Y)")
        L.append(f"<b>CAGR: {s2}{perf['cagr']:.1f}%</b>{note}")
    return "\n".join(L).strip()


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
    hist = strategy.load_price_history(expiry, symbols)
    if expiry not in hist:
        raise strategy.StrategyError(f"No bhavcopy for expiry {expiry}")
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(expiry))
    signals = strategy.compute_signals_cached(hist, fo, expiry, symbols)
    basket, full = strategy.rank_universe(signals, sectors)

    kept, dropped, added, veto_ran = (basket["symbol"].tolist(), [], [], True)
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

    if config.VETO_ENABLED:
        try:
            import surveillance
            kept, dropped, added, veto_ran = surveillance.apply_veto(
                basket, list(full.index), sectors, session)
        except Exception as exc:
            logger.error("Veto failed, proceeding without it: %s", exc)
            veto_ran = False

    band = config.ENTRY_BAND_PCT / 100.0
    stop_pct_sheet = strategy.resolve_stop_pct(expiry, symbols, hist)
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
        lo, hi = close * (1 - band), close * (1 + band)

        feat = llm_judgment.build_features(sym, expiry, hist, entry=close,
                                           signals=signals,
                                           universe_stats=universe_stats)
        tgt = llm_judgment.get_or_set_target(sym, close, expiry, feat)
        target = tgt["target_pct"] / 100.0

        rows.append({
            "symbol": sym,
            "sector": sectors.get(sym, "Unclassified"),
            "close": close,
            "entry_lo": lo, "entry_hi": hi,
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

    return {"expiry": expiry, "rows": rows, "dropped": dropped,
            "veto_ran": veto_ran, "sells": sells, "holds": to_hold,
            "had_prior_book": bool(current), "stop_pct": stop_pct_sheet}


def render_entry_sheet(sheet: dict) -> str:
    from alerts import esc
    rows = sheet["rows"]
    weight = 100.0 / config.PORTFOLIO_SIZE
    sells = sheet.get("sells") or []
    holds = sheet.get("holds") or []
    buys = [r for r in rows if r.get("action") != "HOLD"]

    L = [f"<b>Portfolio for this month</b>",
         f"<i>Basket from the {sheet['expiry']:%d-%m-%y} close — "
         f"place at the next open</i>",
         ""]

    # Exits first: the money has to come out before it can go back in.
    if sells:
        L.append("<b>SELL ORDERS - AT MARKET ON OPEN</b>")
        for i, s in enumerate(sells, 1):
            pnl = ""
            if s.get("pnl_pct") is not None:
                sign = "+" if s["pnl_pct"] >= 0 else ""
                pnl = f"  ({sign}{s['pnl_pct']:.1f}%)"
            L.append(f"{i}. {esc(s['symbol'])} - dropped out of the basket{pnl}")
        L.append("")

    if holds:
        L.append("<b>CONTINUE TO HOLD - no order needed</b>")
        for i, sym in enumerate(holds, 1):
            L.append(f"{i}. {esc(sym)}")
        L.append("")

    if buys:
        L.append(f"<b>BUY ORDERS - invest {weight:.0f}% in each</b>")
        L.append("")
    for i, r in enumerate(buys, 1):
        L.append(f"<b>{i}. {esc(r['symbol'])}</b>")
        L.append(f"    Enter at market: {_fmt_money(r['entry_lo'])} – "
                 f"{_fmt_money(r['entry_hi'])}")
        L.append(f"    SL @{sheet.get('stop_pct', config.V4_STOP_LOSS_PCT):.0f}%: "
                 f"{_fmt_money(r['sl_lo'])} – {_fmt_money(r['sl_hi'])}")
        tp = r.get("target_pct", config.V4_TARGET_PCT)
        L.append(f"    Book at +{tp:.0f}%: "
                 f"{_fmt_money(r['tgt_lo'])} – {_fmt_money(r['tgt_hi'])}")
        L.append("")

    if not sheet.get("had_prior_book", True):
        L.append("<i>No prior positions found — treating every name above "
                 "as a fresh buy.</i>")
        L.append("")

    L.append(f"<i>Place the {sheet.get('stop_pct', config.V4_STOP_LOSS_PCT):.0f}% SL "
             f"as a resting order once the buy fills. Stop width is set by "
             f"market breadth on the expiry close.</i>")
    L.append("")
    L.append(f"<i>The +{config.V4_TARGET_PCT:.0f}% target CANNOT be placed "
             f"today. F&amp;O scrips have a dynamic price band of "
             f"±{config.PRICE_BAND_PCT:.0f}% of the previous close and the "
             f"exchange rejects anything outside it. The evening note will "
             f"tell you the day each target comes within range.</i>")
    if sheet["dropped"]:
        L.append("")
        L.append("<b>Excluded by surveillance:</b>")
        for sym, why in sheet["dropped"]:
            L.append(f"  {esc(sym)} — {esc(why)}")
    if not sheet["veto_ran"]:
        L.append("")
        L.append("<i>⚠ Surveillance check did not run</i>")
    return "\n".join(L).strip()


EXIT_LABEL = {
    "STOP": "STOPLOSS",
    "TARGET": "Target exit",
    "ROLLOVER": "Momentum exit",
}


def render(rpt: Report) -> str:
    """
    Telegram HTML, deliberately terse -- it is read on a phone before
    market open and every line should map to an action or a number.

    Five sections, each shown only when it has content:
        header · month to date · exits today · sell orders ·
        buy orders · continue to hold
    """
    from alerts import esc
    L = [f"<b>Momentum Tracker — {rpt.as_of:%d-%m-%y}</b>", ""]

    sign = "+" if rpt.mtd_return_pct >= 0 else ""
    L.append(f"<b>Cycle performance: {sign}{rpt.mtd_return_pct:.2f}%</b>")
    L.append(f"<i>since the {rpt.expiry:%d-%m-%y} expiry</i>")
    L.append("")

    today_exits = [e for e in rpt.exits if e.exit_date == rpt.as_of]
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
            elif o["kind"] == "OFF_MOMENTUM":
                L.append(f"{esc(o['symbol'])} - OFF MOMENTUM - AT MARKET")
            else:
                L.append(f"{esc(o['symbol'])} - MOMENTUM - AT MARKET")
        L.append("")

    if rpt.buy_orders:
        L.append("<b>BUY ORDERS - PLACE NOW</b>")
        for o in rpt.buy_orders:
            L.append(f"{esc(o['symbol'])} - MARKET RANGE "
                     f"{_fmt_money(o['lo'])}-{_fmt_money(o['hi'])} "
                     f"STOP LOSS - "
                     f"{_fmt_money(o['sl_lo'])}-{_fmt_money(o['sl_hi'])}")
        L.append("")

    if rpt.holdings:
        L.append("<b>CONTINUE TO HOLD</b>")
        for h in sorted(rpt.holdings, key=lambda x: -x.pnl_pct):
            s = "+" if h.pnl_pct >= 0 else ""
            L.append(f"{esc(h.symbol)}  {_fmt_money(h.last)}  "
                     f"({s}{h.pnl_pct:.1f}%)")
        L.append("")

    if rpt.exited_review:
        L.append("<b>Exited</b>")
        for e in rpt.exited_review:
            x = f"{'+' if e['exit_pct'] >= 0 else ''}{e['exit_pct']:.1f}%"
            if e["now_pct"] is None:
                L.append(f"{esc(e['symbol'])} (exited at {x})")
                continue
            y = f"{'+' if e['now_pct'] >= 0 else ''}{e['now_pct']:.1f}%"
            worse = e["now_pct"] > e["exit_pct"]
            L.append(f"{esc(e['symbol'])} (exited at {x}, today at {y})"
                     + ("  \u2190 left on the table" if worse else ""))

    return "\n".join(L).strip()
