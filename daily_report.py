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


def _simulate_to_date(ranked_order, price_by_date, hold_dates, sector_map,
                      basket_symbols, top_n=None, stop_pct=None,
                      target_pct=None, policy=None):
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
    signals = strategy.compute_signals(hist, fo, expiry, symbols)
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
        list(full.index), merged, days, sectors, basket_symbols)

    rpt = Report(as_of=as_of, expiry=expiry, entry_date=days[0],
                 holdings=holdings, exits=exits, to_buy=to_buy,
                 mtd_return_pct=mtd,
                 empty_slots=config.PORTFOLIO_SIZE - len(holdings) - len(to_buy))

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
    signals = strategy.compute_signals(hist, fo, expiry, symbols)
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
        })
    return {"expiry": expiry, "rows": rows, "dropped": dropped,
            "veto_ran": veto_ran}


def render_entry_sheet(sheet: dict) -> str:
    from alerts import esc
    rows = sheet["rows"]
    weight = 100.0 / config.PORTFOLIO_SIZE
    L = [f"<b>Portfolio for this month</b>",
         f"<i>Basket from the {sheet['expiry']:%d-%m-%y} close — "
         f"place at the next open</i>",
         "",
         f"<b>Invest {weight:.0f}% in each of the following:</b>",
         ""]
    for i, r in enumerate(rows):
        tag = LETTERS[i] if i < len(LETTERS) else str(i + 1)
        L.append(f"<b>{tag}. {esc(r['symbol'])}</b>")
        L.append(f"    Enter at market: {_fmt_money(r['entry_lo'])} – "
                 f"{_fmt_money(r['entry_hi'])}")
        L.append(f"    SL @{config.V4_STOP_LOSS_PCT:.0f}%: "
                 f"{_fmt_money(r['sl_lo'])} – {_fmt_money(r['sl_hi'])}")
        L.append(f"    Target @{config.V4_TARGET_PCT:.0f}%: "
                 f"{_fmt_money(r['tgt_lo'])} – {_fmt_money(r['tgt_hi'])}")
        L.append("")

    L.append("<i>Place SL and target as resting orders straight after the "
             "buy fills. Both may trigger intra-day.</i>")
    if sheet["dropped"]:
        L.append("")
        L.append("<b>Excluded by surveillance:</b>")
        for sym, why in sheet["dropped"]:
            L.append(f"  {esc(sym)} — {esc(why)}")
    if not sheet["veto_ran"]:
        L.append("")
        L.append("<i>⚠ Surveillance check did not run</i>")
    return "\n".join(L).strip()


def render(rpt: Report) -> str:
    """Telegram HTML. Kept narrow -- it is read on a phone."""
    from alerts import esc
    L = []
    L.append(f"<b>Momentum Tracker — {rpt.as_of:%d-%m-%y}</b>")
    L.append(f"<i>Basket from the {rpt.expiry:%d-%m-%y} expiry, "
             f"entered {rpt.entry_date:%d-%m-%y}</i>")
    L.append("")

    sign = "+" if rpt.mtd_return_pct >= 0 else ""
    L.append(f"<b>Month to date: {sign}{rpt.mtd_return_pct:.2f}%</b>")
    L.append("<i>equal weight, perfect execution, before costs</i>")
    L.append("")

    today_exits = [e for e in rpt.exits if e.exit_date == rpt.as_of]
    if today_exits:
        L.append("<b>EXITED TODAY — already filled by your broker</b>")
        for e in today_exits:
            s = "+" if e.pnl_pct >= 0 else ""
            L.append(f"  {esc(e.symbol)} — {e.reason} @ {_fmt_money(e.exit_px)} "
                     f"({s}{e.pnl_pct:.1f}%)")
        L.append("")

    if rpt.to_buy:
        L.append("<b>BUY at tomorrow's open</b>")
        for sym in rpt.to_buy:
            L.append(f"  {esc(sym)} — market on open, then set "
                     f"{config.V4_STOP_LOSS_PCT:.0f}% stop / "
                     f"{config.V4_TARGET_PCT:.0f}% target as resting orders")
        L.append("")

    if rpt.to_sell:
        L.append("<b>SELL at tomorrow's open</b>")
        for sym in rpt.to_sell:
            L.append(f"  {esc(sym)} — dropped out of the basket")
        L.append("")

    if rpt.holdings:
        L.append(f"<b>HOLDING ({len(rpt.holdings)})</b>")
        for h in sorted(rpt.holdings, key=lambda x: -x.pnl_pct):
            s = "+" if h.pnl_pct >= 0 else ""
            L.append(f"  {esc(h.symbol)}  {_fmt_money(h.last)}  "
                     f"({s}{h.pnl_pct:.1f}%)  stop {_fmt_money(h.stop)}")
        L.append("")

    earlier = [e for e in rpt.exits if e.exit_date != rpt.as_of]
    if earlier:
        wins = sum(1 for e in earlier if e.pnl_pct > 0)
        L.append(f"<i>Earlier this month: {len(earlier)} closed, "
                 f"{wins} profitable</i>")
        L.append("")

    if rpt.flagged_actions:
        L.append("<b>⚠ Corporate actions needing review</b>")
        for f in rpt.flagged_actions:
            L.append(f"  {esc(f['symbol'])} {f['date']} — "
                     f"{esc(f['classification'])}, ratio "
                     f"{f['adjustment_ratio']:.4f} vs observed "
                     f"{f['observed_ratio']:.4f}")
        L.append("")

    if rpt.veto_dropped:
        L.append("<b>Surveillance veto</b>")
        for sym, why in rpt.veto_dropped:
            L.append(f"  {esc(sym)} excluded — {esc(why)}")
        L.append("")
    if not rpt.veto_ran:
        L.append("<i>⚠ Surveillance check did not run (feed unavailable)</i>")
        L.append("")

    if rpt.empty_slots > 0:
        L.append(f"<i>⚠ {rpt.empty_slots} slot(s) unfilled — no eligible "
                 f"name passed the sector cap</i>")

    return "\n".join(L).strip()
