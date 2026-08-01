"""
V4 momentum strategy: volatility-led selection, derivatives-informed,
with resting stop/target orders and same-slot redeployment.

WHAT THIS IS
------------
Once a month, on the evening of F&O expiry, rank the NSE F&O universe on

    score = 0.50*z(realised volatility) + 0.30*z(rollover %) + 0.20*z(cost of carry)

take the top 10 subject to a 30% sector cap, and buy them at the next
session's open. Each position carries a resting 5% stop-loss and a 40%
target. When a position exits mid-month, its slot is refilled at the NEXT
session's open with the highest-ranked name still available. Everything
still open is sold at the open after the following expiry.

WHY VOLATILITY LEADS
--------------------
Across 13 months / 2677 stock-months, realised volatility was the only
feature whose top decile held meaningfully more winners than chance
(lift 1.5-2.0x). Rollover % alone scored BELOW chance (0.86x). The
derivatives signals still earn their 50% weight combined, but they do not
lead. This inverts what the source deck's marketing implies.

HONESTY NOTE
------------
13 monthly observations. Best t-stat obtained is ~1.2 against a ~2.0
significance bar, so nothing here is statistically established. The
parameters were chosen on this sample and will be partly fitted to it.
Extend the history before trusting the numbers -- see CONTEXT.md.
"""

import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

import config
import nse_client
import scoring

logger = logging.getLogger("momentum_tracker.strategy")


class StrategyError(RuntimeError):
    """Raised when the strategy cannot be evaluated for a given date."""


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def expiry_for(year: int, month: int, trading_days=None) -> date:
    """
    Monthly F&O expiry. Last Thursday up to Aug-2025, last Tuesday from
    Sep-2025 (NSE rule change effective 01-Sep-2025). If the date is not a
    trading day it rolls BACK to the previous one -- e.g. Mar-2026's last
    Tuesday (31st) was Mahavir Jayanti, so expiry was the 30th.

    trading_days: optional set/collection of date objects. When omitted the
    holiday roll-back is skipped and the raw weekday is returned.
    """
    wd = (config.EXPIRY_WEEKDAY_BEFORE
          if (year, month) < config.EXPIRY_RULE_CHANGE
          else config.EXPIRY_WEEKDAY_AFTER)
    d = _last_weekday(year, month, wd)
    if trading_days is None:
        return d
    guard = 0
    while d not in trading_days:
        d -= timedelta(days=1)
        guard += 1
        if guard > 10:
            raise StrategyError(f"no trading day found near {year}-{month:02d} expiry")
    return d


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def known_trading_days() -> set:
    """
    Trading days inferred from cached CM bhavcopy filenames, no network. Used
    to give expiry_for() the holiday roll-back it needs -- without this, a
    month whose last Tuesday/Thursday is a holiday (e.g. 31-Mar-2026,
    Mahavir Jayanti) resolves to a date with no bhavcopy and the run fails.
    """
    days = set()
    if not os.path.isdir(config.CACHE_DIR):
        return days
    for fn in os.listdir(config.CACHE_DIR):
        if fn.startswith("cm_") and fn.endswith(".csv"):
            try:
                from datetime import datetime as _dt
                days.add(_dt.strptime(fn[3:-4], "%Y%m%d").date())
            except ValueError:
                continue
    return days


def load_fo_universe(path: str = None) -> list:
    """
    Parse NSE's fo_mktlots.csv into the list of symbols that currently have
    a live stock-futures contract. The file has an index block first, then a
    row whose second column is literally 'Symbol', then one row per stock.
    A blank lot size in the front month means no live contract.
    """
    path = path or config.FO_MKTLOTS_FILE
    if not os.path.exists(path):
        raise StrategyError(
            f"F&O universe file not found: {path}. Download it from "
            "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
        )
    with open(path) as fh:
        rows = list(csv.reader(fh))
    try:
        start = next(i + 1 for i, r in enumerate(rows)
                     if len(r) > 1 and r[1].strip() == "Symbol")
    except StopIteration:
        raise StrategyError(f"{path} is not in the expected fo_mktlots format")
    symbols = sorted({
        r[1].strip() for r in rows[start:]
        if len(r) > 2 and r[1].strip() and r[2].strip()
    })
    if not symbols:
        raise StrategyError(f"{path} parsed to an empty universe")
    logger.info("Loaded %d F&O-eligible symbols from %s", len(symbols), path)
    return symbols


def load_sector_map() -> dict:
    if not os.path.exists(config.SECTOR_MAP_FILE):
        logger.warning("Sector map missing at %s -- sector cap disabled",
                       config.SECTOR_MAP_FILE)
        return {}
    df = pd.read_csv(config.SECTOR_MAP_FILE)
    return dict(zip(df["symbol"].str.strip().str.upper(), df["sector"]))


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

def split_adjust(closes: list, symbol: str = None, dates: list = None,
                 session=None, flagged: list = None) -> list:
    """
    Back-adjust a close series for corporate actions.

    NSE bhavcopy is NOT corporate-action adjusted: MCX 5:1 on 02-Jan-2026
    shows as a one-day -80% move and would otherwise be scored as the
    universe's worst momentum AND its highest volatility. Both were wrong.

    When `symbol` and `dates` are supplied and the classifier is enabled,
    each ratio breach is explained against NSE's actual corporate action
    filings (see corporate_actions.py) rather than blindly assumed to be
    a split. That distinction matters: without it, a genuine -80% collapse
    is silently laundered into a clean series and the stock then scores as
    high-volatility and enters the basket.

    Without those arguments the legacy heuristic applies unchanged, so
    existing callers and tests keep their exact previous behaviour.

    `flagged`, if given, receives the classifier result dict for any
    breach whose ratio did not reconcile with the observed move, so the
    caller can surface it in the evening note.
    """
    out = list(closes)
    use_classifier = bool(symbol and dates and config.CORP_ACTION_LLM_ENABLED
                          and len(dates) == len(closes))

    for i in range(1, len(out)):
        if out[i - 1] <= 0:
            continue
        ratio = out[i] / out[i - 1]
        if config.V4_SPLIT_RATIO_LOW <= ratio <= config.V4_SPLIT_RATIO_HIGH:
            continue

        adjustment = ratio          # legacy default
        if use_classifier:
            try:
                import corporate_actions
                verdict = corporate_actions.classify(
                    symbol, dates[i], out[i - 1], out[i], session=session)
            except Exception as exc:
                # Classification must never abort a strategy run.
                logger.error("Classifier failed for %s at %s (%s); using "
                             "heuristic", symbol, dates[i], exc)
                verdict = None

            if verdict is not None:
                kind = verdict["classification"]
                if verdict.get("flagged") and flagged is not None:
                    flagged.append(verdict)
                if kind == "GENUINE_MOVE":
                    logger.info("%s %s: genuine %.1f%% move, NOT adjusted",
                                symbol, dates[i], (ratio - 1) * 100)
                    continue
                if kind == "UNKNOWN":
                    if config.CORP_ACTION_UNKNOWN_POLICY == "no_adjust":
                        logger.warning("%s %s: unexplained breach, not adjusted",
                                       symbol, dates[i])
                        continue
                    logger.warning("%s %s: unexplained breach, falling back to "
                                   "heuristic ratio %.4f", symbol, dates[i], ratio)
                else:
                    adjustment = verdict["adjustment_ratio"]
                    logger.info("%s %s: %s, adjusting prior closes by %.4f",
                                symbol, dates[i], kind, adjustment)

        logger.debug("Corporate action at index %d (ratio %.3f -> adj %.4f)",
                     i, ratio, adjustment)
        for j in range(i):
            out[j] *= adjustment
    return out


def load_price_history(as_of: date, symbols, days: int = None) -> dict:
    """
    Fetch `days` trading days of cash-market bhavcopy up to and including
    as_of. Returns {date: DataFrame indexed by symbol}. Days that cannot be
    fetched (holidays, gaps) are skipped and logged rather than raising --
    a single missing day must not abort a run.
    """
    days = days or config.V4_HISTORY_DAYS
    out, misses = {}, 0
    d = as_of
    # normalize_cm_columns only maps close/volume; the simulator needs OHLC
    # because the resting stop and target trigger on the day's low/high.
    ohlc = {"OpnPric": "open_price", "HghPric": "high_price", "LwPric": "low_price"}
    while len(out) < days:
        if d.weekday() < 5:
            try:
                raw = nse_client.fetch_cm_bhavcopy(d)
                norm = scoring.normalize_cm_columns(raw)
                # raw can carry >1 row per symbol (dual series, e.g. IIFL
                # trading under both EQ and BL) even after normalize_cm_columns
                # filters its own copy -- dedupe on the same key before
                # reindexing or .reindex() raises on duplicate labels.
                raw_ohlc = raw.drop_duplicates(subset=["TckrSymb"], keep="first")
                for src, dst in ohlc.items():
                    if src in raw_ohlc.columns and dst not in norm.columns:
                        norm[dst] = raw_ohlc.set_index("TckrSymb")[src].reindex(norm["symbol"]).values
                out[d] = norm.set_index("symbol")
            except nse_client.NseFetchError as exc:
                misses += 1
                logger.debug("No CM bhavcopy for %s (%s)", d, exc)
        d -= timedelta(days=1)
        if (as_of - d).days > days * 3:
            break
    if not out:
        raise StrategyError(f"No cash-market data available up to {as_of}")
    logger.info("Loaded %d trading days of prices up to %s (%d dates skipped)",
                len(out), as_of, misses)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def signals_cache_path(snapshot: date) -> str:
    d = os.path.join(config.DATA_DIR, "signals_cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"sig_{snapshot:%Y%m%d}.csv")


def compute_signals_cached(price_hist: dict, fo_today: pd.DataFrame,
                           snapshot: date, symbols, flagged: list = None):
    """
    compute_signals with a disk cache keyed on the snapshot date.

    A past expiry's signals are a pure function of bhavcopy that is
    already published, so they never change. Caching turns a 13-month
    backtest from ~200s of recomputation into a few seconds on re-runs,
    which matters because the run is now doing network + LLM work inside
    split_adjust.

    Cache misses fall through to the real computation. A corrupt or
    unreadable cache entry is ignored rather than fatal.
    """
    path = signals_cache_path(snapshot)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, index_col=0)
            logger.info("Signals cache hit for %s (%d symbols)", snapshot, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable signals cache %s: %s", path, exc)
    df = compute_signals(price_hist, fo_today, snapshot, symbols, flagged=flagged)
    try:
        df.to_csv(path)
    except OSError as exc:
        logger.warning("Could not write signals cache %s: %s", path, exc)
    return df


def compute_signals(price_hist: dict, fo_today: pd.DataFrame, snapshot: date,
                    symbols, flagged: list = None) -> pd.DataFrame:
    """
    One row per symbol: volatility, rollover %, cost of carry, DMAs.

    `flagged`, if given, collects corporate-action verdicts whose ratio
    did not reconcile with the observed price move, for the evening note.
    """
    dates = sorted(price_hist)
    spot = price_hist[snapshot]["close_price"] if snapshot in price_hist else None
    if spot is None:
        raise StrategyError(f"{snapshot} is not in the loaded price history")

    rows = []
    for sym in symbols:
        closes, vols, close_dates = [], [], []
        for d in dates:
            frame = price_hist[d]
            if sym not in frame.index:
                continue
            c = frame.at[sym, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            closes.append(float(c))
            close_dates.append(d)
            vols.append(frame.at[sym, "volume"] if "volume" in frame.columns else np.nan)
        if len(closes) < config.V4_VOL_LOOKBACK_DAYS - 10:
            continue
        closes = split_adjust(closes, symbol=sym, dates=close_dates,
                              flagged=flagged)
        s = pd.Series(closes)
        rets = s.pct_change().dropna().tail(config.V4_VOL_LOOKBACK_DAYS)
        rows.append({
            "symbol": sym,
            "close": closes[-1],
            "volatility": float(rets.std() * np.sqrt(252) * 100),
            "dma10": s.tail(10).mean(),
            "dma20": s.tail(20).mean(),
            "dma50": s.tail(50).mean(),
        })
    df = pd.DataFrame(rows).set_index("symbol")
    if df.empty:
        raise StrategyError("No symbol had enough price history to score")

    fut = fo_today[fo_today["instrument_type"].isin(["STF", "FUTSTK"])].copy()
    rollover, carry = {}, {}
    for sym, grp in fut.groupby("symbol"):
        g = grp.sort_values("expiry_date")
        if len(g) >= 2:
            total = g.iloc[0]["open_interest"] + g.iloc[1]["open_interest"]
            if total > 0:
                rollover[sym] = g.iloc[1]["open_interest"] / total * 100
        forward = g[g["expiry_date"].dt.date > snapshot]
        if len(forward):
            near = forward.iloc[0]
            sp = spot.get(sym)
            dte = (near["expiry_date"].date() - snapshot).days
            if pd.notna(sp) and sp > 0 and dte > 0:
                carry[sym] = (near["settlement_price"] - sp) / sp * (365 / dte) * 100
    df["rollover"] = pd.Series(rollover)
    df["cost_of_carry"] = pd.Series(carry)
    return df


def rank_universe(signals: pd.DataFrame, sector_map: dict = None,
                  top_n: int = None) -> pd.DataFrame:
    """Z-score each signal, weight, sort, then apply the sector cap greedily."""
    top_n = top_n or config.PORTFOLIO_SIZE
    df = signals.dropna(subset=["volatility", "rollover", "cost_of_carry"]).copy()
    if len(df) < top_n:
        raise StrategyError(f"only {len(df)} scoreable symbols, need {top_n}")

    def z(col):
        sd = df[col].std()
        return (df[col] - df[col].mean()) / sd if sd else df[col] * 0

    w = config.V4_WEIGHTS
    df["score"] = (w["volatility"] * z("volatility")
                   + w["rollover"] * z("rollover")
                   + w["cost_of_carry"] * z("cost_of_carry"))
    df = df.sort_values("score", ascending=False)

    max_per_sector = max(1, int(top_n * config.MAX_SECTOR_WEIGHT_PCT / 100))
    chosen, counts = [], {}
    for sym in df.index:
        sec = (sector_map or {}).get(sym, f"Unclassified:{sym}")
        if sector_map and counts.get(sec, 0) >= max_per_sector:
            continue
        chosen.append(sym)
        counts[sec] = counts.get(sec, 0) + 1
        if len(chosen) >= top_n:
            break

    out = df.loc[chosen].reset_index()
    out.insert(0, "rank", range(1, len(out) + 1))
    out["weight_pct"] = round(100 / top_n, 2)
    out["stop_loss"] = (out["close"] * (1 - config.V4_STOP_LOSS_PCT / 100)).round(2)
    out["target"] = (out["close"] * (1 + config.V4_TARGET_PCT / 100)).round(2)
    return out, df


# ---------------------------------------------------------------------------
# Portfolio simulation
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    entry: float
    stop: float
    target: float
    entry_date: date = None


@dataclass
class MonthResult:
    month: str
    entry_date: date
    exit_date: date
    return_pct: float
    trades: int
    slots: list = field(default_factory=list)
    carry: dict = field(default_factory=dict)


def simulate_month(ranked_order, price_by_date, hold_dates, sector_map,
                   reentry_policy=None, top_n=None,
                   stop_pct=None, target_pct=None,
                   carry_in: dict = None, basket_symbols=None,
                   carry_forward: bool = None) -> MonthResult:
    """
    Day-by-day simulation of `top_n` equally weighted slots.

    All slots advance through the calendar in lockstep. This matters: an
    earlier implementation ran each slot's full chain to month-end before
    starting the next, which let one slot claim a replacement that another
    slot had actually needed earlier in calendar time, and (in the
    re-entry variants) let the same stock occupy several slots at once.
    That inflated the backtest by ~6.5 percentage points.

    Stop and target are resting orders and may fill intra-day. A slot
    freed by an exit is refilled at the NEXT session's open -- never the
    same day, since you cannot know at 09:15 that a stop will trigger at
    14:00.

    CROSS-MONTH REBALANCING (carry_forward, "v5"):
    `carry_in` is {symbol: Position} handed back as `.carry` from the
    previous month's call. A carried symbol that is still in
    `basket_symbols` (this month's capped top-N) is re-slotted with its
    OLD cost basis -- no sell, no rebuy, only its stop/target reset off
    that basis for this month's stop_pct/target_pct. A carried symbol
    that dropped out of the basket is sold at this month's entry-day
    open (reason "ROLLOVER") and its slot redeployed the next session,
    identically to a mid-month stop/target exit. Positions still open at
    this month's end are NOT force-sold -- they're marked to the final
    close and returned via `.carry` for the caller to hand to next
    month's call (or liquidate, if there is no next month).
    When `carry_forward` is False, behaviour is exactly the pre-v5
    engine: 10 empty slots every month, everything force-sold at the
    final day's open.
    """
    top_n = top_n or config.PORTFOLIO_SIZE
    stop_pct = config.V4_STOP_LOSS_PCT if stop_pct is None else stop_pct
    target_pct = config.V4_TARGET_PCT if target_pct is None else target_pct
    policy = reentry_policy or config.V4_REENTRY_POLICY
    carry_forward = config.V4_CARRY_FORWARD if carry_forward is None else carry_forward
    max_per_sector = max(1, int(top_n * config.MAX_SECTOR_WEIGHT_PCT / 100))

    held = {i: None for i in range(top_n)}
    sector_count, banned, pending = {}, set(), []
    pnl = [0.0] * top_n
    chains = [[] for _ in range(top_n)]
    trades = 0

    def sector_of(sym):
        return (sector_map or {}).get(sym, f"Unclassified:{sym}")

    def available(sym):
        if sym in banned:
            return False
        if any(p and p.symbol == sym for p in held.values()):
            return False
        if sector_map and sector_count.get(sector_of(sym), 0) >= max_per_sector:
            return False
        return True

    def open_position(slot, sym, day, basis=None, basis_date=None):
        if basis is not None:
            held[slot] = Position(sym, float(basis),
                                  float(basis) * (1 - stop_pct / 100),
                                  float(basis) * (1 + target_pct / 100),
                                  basis_date)
            sector_count[sector_of(sym)] = sector_count.get(sector_of(sym), 0) + 1
            return True
        frame = price_by_date.get(day)
        if frame is None or sym not in frame.index:
            return False
        px_open = frame.at[sym, "open_price"]
        if pd.isna(px_open) or px_open <= 0:
            return False
        held[slot] = Position(sym, float(px_open),
                              float(px_open) * (1 - stop_pct / 100),
                              float(px_open) * (1 + target_pct / 100), day)
        sector_count[sector_of(sym)] = sector_count.get(sector_of(sym), 0) + 1
        return True

    first = hold_dates[0]
    frame0 = price_by_date.get(first)
    basket_set = (set(basket_symbols) if basket_symbols is not None
                  else set(ranked_order[:top_n]))
    slot = 0

    if carry_forward and carry_in:
        # Re-slot carried positions that are still in this month's basket --
        # no trade, no new cost basis.
        for sym, pos in carry_in.items():
            if slot >= top_n:
                break
            if sym in basket_set and available(sym):
                open_position(slot, sym, first, basis=pos.entry, basis_date=pos.entry_date)
                slot += 1

        # Everything else that was open is sold at today's open (a real
        # trade, tagged ROLLOVER so it's distinguishable from STOP/TARGET),
        # then its slot is queued for next-session redeployment.
        for sym, pos in carry_in.items():
            if sym in basket_set:
                continue
            if slot >= top_n:
                break
            if frame0 is None or sym not in frame0.index:
                continue
            px_open = frame0.at[sym, "open_price"]
            if pd.isna(px_open) or px_open <= 0:
                continue
            ret = (float(px_open) - pos.entry) / pos.entry * 100
            use_slot = slot
            slot += 1
            pnl[use_slot] += ret
            trades += 1
            chains[use_slot].append((sym, round(ret, 2), "ROLLOVER", str(first)))
            if policy != "always":
                banned.add(sym)
            for cand in ranked_order:
                if available(cand) and cand not in [s for _, s in pending]:
                    pending.append((use_slot, cand))
                    break

    # Fill whatever slots are still empty, same as a no-carry month.
    for sym in ranked_order:
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
            exit_px, reason = None, None
            if pd.notna(low) and low <= pos.stop:
                exit_px, reason = pos.stop, "STOP"
            elif pd.notna(high) and high >= pos.target:
                exit_px, reason = pos.target, "TARGET"
            if exit_px is None:
                continue

            ret = (exit_px - pos.entry) / pos.entry * 100
            pnl[slot_id] += ret
            trades += 1
            chains[slot_id].append((pos.symbol, round(ret, 2), reason, str(day)))
            sector_count[sector_of(pos.symbol)] -= 1
            if policy == "never" or (policy == "not_if_stopped" and reason == "STOP"):
                banned.add(pos.symbol)
            held[slot_id] = None

            if i < len(hold_dates) - 1:
                for cand in ranked_order:
                    if available(cand) and cand not in [s for _, s in pending]:
                        pending.append((slot_id, cand))
                        break

    final = hold_dates[-1]
    frame = price_by_date.get(final)
    carry_out = {}
    if carry_forward:
        # Mark still-open positions to the close -- nothing is actually
        # sold, so this is not a trade and does not touch `trades`. The
        # resulting basis is handed back so next month's call can decide
        # HOLD vs SELL against the new basket.
        for slot_id in range(top_n):
            pos = held.get(slot_id)
            if pos is None or frame is None or pos.symbol not in frame.index:
                continue
            px_close = frame.at[pos.symbol, "close_price"]
            if pd.isna(px_close) or px_close <= 0:
                continue
            ret = (float(px_close) - pos.entry) / pos.entry * 100
            pnl[slot_id] += ret
            chains[slot_id].append((pos.symbol, round(ret, 2), "MARK", str(final)))
            carry_out[pos.symbol] = Position(
                pos.symbol, float(px_close),
                float(px_close) * (1 - stop_pct / 100),
                float(px_close) * (1 + target_pct / 100), final)
    else:
        # Pre-v5 behaviour: force-sell everything at the final day's open.
        for slot_id in range(top_n):
            pos = held.get(slot_id)
            if pos is None or frame is None or pos.symbol not in frame.index:
                continue
            px_open = frame.at[pos.symbol, "open_price"]
            if pd.isna(px_open):
                continue
            ret = (float(px_open) - pos.entry) / pos.entry * 100
            pnl[slot_id] += ret
            trades += 1
            chains[slot_id].append((pos.symbol, round(ret, 2), "EXPIRY", str(final)))

    return MonthResult(
        month=f"{hold_dates[0]:%Y-%m}",
        entry_date=hold_dates[0],
        exit_date=hold_dates[-1],
        return_pct=float(np.mean(pnl)),
        trades=trades,
        slots=chains,
        carry=carry_out,
    )
