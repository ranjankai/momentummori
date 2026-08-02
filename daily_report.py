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
data/v4_holdings.json is hand-maintained and its `entry` fields are None
until you type in a fill price. A nightly job cannot depend on that. So
this module replays the month deterministically from the governing
expiry's basket plus cached bhavcopy, which needs no bookkeeping and
cannot drift out of sync with reality.

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


@dataclass
class Holding:
    symbol: str
    entry: float
    entry_date: date
    stop: float
    target: float
    last: float = None

    @property
    def pnl_pct(self) -> float:
        if not self.entry:
            return 0.0
        return (self.last - self.entry) / self.entry * 100

    @property
    def pct_to_target(self) -> float:
        """How far the stock still has to travel to reach the target."""
        if not self.last:
            return 0.0
        return (self.target - self.last) / self.last * 100

    @property
    def target_placeable(self) -> bool:
        """
        True once a sell limit at `target` is inside the exchange's dynamic
        price band, measured off the latest close. Below this it is
        rejected at order entry, which is why the target is NOT placed on
        entry day.
        """
        if not self.last:
            return False
        return self.target <= self.last * (1 + config.PRICE_BAND_PCT / 100.0)


@dataclass
class Exit:
    symbol: str
    entry: float
    exit_px: float
    reason: str          # STOP | TARGET | ROLLOVER
    exit_date: date

    @property
    def pnl_pct(self) -> float:
        return (self.exit_px - self.entry) / self.entry * 100


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


def governing_expiry(as_of: date, trading_days=None) -> date:
    """
    The expiry whose basket you are currently holding: the most recent
    monthly expiry strictly before `as_of`. Positions bought the session
    after that expiry are the ones live today.
    """
    y, m = as_of.year, as_of.month
    exp = strategy.expiry_for(y, m, trading_days=trading_days)
    if exp >= as_of:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        exp = strategy.expiry_for(y, m, trading_days=trading_days)
    return exp


def load_actual_fills() -> dict:
    """
    Recorded real fills, {SYMBOL: {"entry": float, "entry_date": "YYYY-MM-DD"}}.

    Empty dict when the file is absent or unreadable -- the report then
    falls back to the reconstructed open, which is right whenever you
    traded on schedule.
    """
    import json
    path = config.ACTUAL_FILLS_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return {str(k).strip().upper(): v for k, v in raw.items()}
    except (OSError, ValueError) as exc:
        logger.error("Unreadable %s (%s) -- falling back to reconstructed "
                     "entries. Stops and targets WILL be wrong if you "
                     "entered off-schedule.", path, exc)
        return {}


def _simulate_to_date(ranked_order, price_by_date, hold_dates, sector_map,
                      basket_symbols, top_n=None, stop_pct=None,
                      target_pct=None, policy=None, fills=None):
    """
    Lockstep slot replay from entry day to `hold_dates[-1]`, retaining the
    ORIGINAL cost basis of every open position (which simulate_month
    discards when it marks to close for carry-forward).

    Mirrors strategy.simulate_month exactly in its trading rules --
    same lockstep advance, same next-session redeployment, same re-entry
    policy. tests assert the resulting return matches.
    """
    top_n = top_n or config.PORTFOLIO_SIZE
    stop_pct = config.V4_STOP_LOSS_PCT if stop_pct is None else stop_pct
    target_pct = config.V4_TARGET_PCT if target_pct is None else target_pct
    policy = policy or config.V4_REENTRY_POLICY
    max_per_sector = max(1, int(top_n * config.MAX_SECTOR_WEIGHT_PCT / 100))

    held = {i: None for i in range(top_n)}
    sector_count, banned, pending = {}, set(), []
    pnl = [0.0] * top_n
    exits = []

    def sector_of(s):
        return (sector_map or {}).get(s, f"Unclassified:{s}")

    def available(s):
        if s in banned:
            return False
        if any(p and p.symbol == s for p in held.values()):
            return False
        if sector_map and sector_count.get(sector_of(s), 0) >= max_per_sector:
            return False
        return True

    def open_position(slot, sym, day):
        # A recorded real fill always wins over the reconstructed open.
        # Everything downstream -- stop, target, P&L -- is derived from
        # the entry price, so using the wrong one produces orders that
        # look plausible and are materially wrong.
        override = (fills or {}).get(sym)
        if override and override.get("entry"):
            px = float(override["entry"])
            entry_day = day
            raw = override.get("entry_date")
            if raw:
                try:
                    entry_day = pd.to_datetime(raw).date()
                except (ValueError, TypeError):
                    logger.warning("Unparseable entry_date %r for %s", raw, sym)
            last = px
            frame = price_by_date.get(day)
            if frame is not None and sym in frame.index:
                c = frame.at[sym, "close_price"]
                if pd.notna(c) and c > 0:
                    last = float(c)
            held[slot] = Holding(sym, px, entry_day,
                                 px * (1 - stop_pct / 100),
                                 px * (1 + target_pct / 100), last)
            sector_count[sector_of(sym)] = sector_count.get(sector_of(sym), 0) + 1
            logger.info("%s: using recorded fill %.2f (%s) instead of the "
                        "reconstructed open", sym, px, entry_day)
            return True

        frame = price_by_date.get(day)
        if frame is None or sym not in frame.index:
            return False
        px = frame.at[sym, "open_price"]
        if pd.isna(px) or px <= 0:
            return False
        px = float(px)
        held[slot] = Holding(sym, px, day,
                             px * (1 - stop_pct / 100),
                             px * (1 + target_pct / 100), px)
        sector_count[sector_of(sym)] = sector_count.get(sector_of(sym), 0) + 1
        return True

    first = hold_dates[0]
    slot = 0
    for sym in basket_symbols:
        if slot >= top_n:
            break
        if available(sym) and open_position(slot, sym, first):
            slot += 1

    for i in range(1, len(hold_dates)):
        day = hold_dates[i]
        for slot_id, sym in pending:
            if available(sym):
                open_position(slot_id, sym, day)
        pending = []

        frame = price_by_date.get(day)
        if frame is None:
            continue
        for slot_id in range(top_n):
            pos = held.get(slot_id)
            if pos is None or pos.symbol not in frame.index:
                continue
            low = frame.at[pos.symbol, "low_price"]
            high = frame.at[pos.symbol, "high_price"]
            close = frame.at[pos.symbol, "close_price"]
            if pd.notna(close) and close > 0:
                pos.last = float(close)

            exit_px, reason = None, None
            if pd.notna(low) and low <= pos.stop:
                exit_px, reason = pos.stop, "STOP"
            elif pd.notna(high) and high >= pos.target:
                exit_px, reason = pos.target, "TARGET"
            if exit_px is None:
                continue

            pnl[slot_id] += (exit_px - pos.entry) / pos.entry * 100
            exits.append(Exit(pos.symbol, pos.entry, exit_px, reason, day))
            sector_count[sector_of(pos.symbol)] -= 1
            if policy == "never" or (policy == "not_if_stopped" and reason == "STOP"):
                banned.add(pos.symbol)
            held[slot_id] = None
            if i < len(hold_dates) - 1:
                for cand in ranked_order:
                    if available(cand) and cand not in [s for _, s in pending]:
                        pending.append((slot_id, cand))
                        break

    # Mark open positions to the last close -- unrealised, but that is
    # what "month to date" means.
    open_positions = []
    for slot_id in range(top_n):
        pos = held.get(slot_id)
        if pos is None:
            continue
        pnl[slot_id] += pos.pnl_pct
        open_positions.append(pos)

    # Names queued for tomorrow's open, plus any slot still empty.
    to_buy = [sym for _, sym in pending]
    empty = top_n - len(open_positions) - len(to_buy)
    if empty > 0:
        for cand in ranked_order:
            if empty <= 0:
                break
            if available(cand) and cand not in to_buy:
                to_buy.append(cand)
                empty -= 1

    return open_positions, exits, to_buy, float(sum(pnl) / top_n)


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

    fwd = strategy.load_price_history(as_of, symbols, days=60)
    merged = dict(hist)
    merged.update(fwd)
    days = [d for d in sorted(merged) if expiry < d <= as_of]
    if not days:
        raise strategy.StrategyError(
            f"No trading days between governing expiry {expiry} and {as_of}")

    holdings, exits, to_buy, mtd = _simulate_to_date(
        list(full.index), merged, days, sectors, basket_symbols,
        fills=load_actual_fills())

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
    stop = config.V4_STOP_LOSS_PCT / 100.0
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

    if config.VETO_ENABLED:
        try:
            import surveillance
            _, dropped, _, ran = surveillance.apply_veto(
                basket, list(full.index), sectors, session)
            rpt.veto_dropped = dropped
            rpt.veto_ran = ran
        except Exception as exc:                      # never break the note
            logger.error("Veto step failed, continuing without it: %s", exc)
            rpt.veto_ran = False
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
    if config.VETO_ENABLED:
        try:
            import surveillance
            kept, dropped, added, veto_ran = surveillance.apply_veto(
                basket, list(full.index), sectors, session)
        except Exception as exc:
            logger.error("Veto failed, proceeding without it: %s", exc)
            veto_ran = False

    band = config.ENTRY_BAND_PCT / 100.0
    stop = config.V4_STOP_LOSS_PCT / 100.0
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
        rows.append({
            "symbol": sym,
            "sector": sectors.get(sym, "Unclassified"),
            "close": close,
            "entry_lo": lo, "entry_hi": hi,
            "sl_lo": lo * (1 - stop), "sl_hi": hi * (1 - stop),
            "tgt_lo": lo * (1 + target), "tgt_hi": hi * (1 + target),
            "action": "HOLD" if sym in current else "BUY",
        })

    sells = []
    for sym in to_sell:
        h = current[sym]
        sells.append({"symbol": sym, "last": h.last, "pnl_pct": h.pnl_pct})

    return {"expiry": expiry, "rows": rows, "dropped": dropped,
            "veto_ran": veto_ran, "sells": sells, "holds": to_hold,
            "had_prior_book": bool(current)}


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
        L.append(f"    SL @{config.V4_STOP_LOSS_PCT:.0f}%: "
                 f"{_fmt_money(r['sl_lo'])} – {_fmt_money(r['sl_hi'])}")
        L.append(f"    Book at +{config.V4_TARGET_PCT:.0f}%: "
                 f"{_fmt_money(r['tgt_lo'])} – {_fmt_money(r['tgt_hi'])} "
                 f"<i>(do not place yet)</i>")
        L.append("")

    if not sheet.get("had_prior_book", True):
        L.append("<i>No prior positions found — treating every name above "
                 "as a fresh buy.</i>")
        L.append("")

    L.append(f"<i>Place the SL as a resting order once the buy fills — it is "
             f"inside the {config.PRICE_BAND_PCT:.0f}% band and will be "
             f"accepted.</i>")
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
    L.append(f"<b>Month to date: {sign}{rpt.mtd_return_pct:.2f}%</b>")
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
            else:
                L.append(f"{esc(o['symbol'])} - MOMENTUM - AT MARKET")
        L.append("")

    if rpt.buy_orders:
        L.append("<b>BUY ORDERS - PLACE NOW</b>")
        for o in rpt.buy_orders:
            L.append(f"{esc(o['symbol'])} - MARKET RANGE "
                     f"{_fmt_money(o['lo'])}-{_fmt_money(o['hi'])} "
                     f"STOP LOSS @{config.V4_STOP_LOSS_PCT:.0f}% - "
                     f"{_fmt_money(o['sl_lo'])}-{_fmt_money(o['sl_hi'])}")
        L.append("")

    if rpt.holdings:
        L.append("<b>CONTINUE TO HOLD</b>")
        for h in sorted(rpt.holdings, key=lambda x: -x.pnl_pct):
            s = "+" if h.pnl_pct >= 0 else ""
            L.append(f"{esc(h.symbol)}  {_fmt_money(h.last)}  "
                     f"({s}{h.pnl_pct:.1f}%)")

    return "\n".join(L).strip()
