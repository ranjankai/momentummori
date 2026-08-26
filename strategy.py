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


def market_breadth(as_of: date, symbols, price_hist: dict) -> float:
    """
    Percentage of the universe trading above its own 200-day average.

    Computed on the expiry close, so it is available BEFORE entry. This
    is the regime signal that picks the stop width -- see
    config.REGIME_STOP_ENABLED and the table in config.py.

    25-Aug-2026 fix: this used to compare RAW, un-split-adjusted closes
    against their own 200-day mean. `split_adjust` was already built and
    already used for the selection signal (line ~599) and for holding
    P&L (`adjust_holding_window`) -- this function was the one place
    still reading straight bhavcopy prices. A stock with any split/bonus
    in the trailing ~200 sessions read as artificially "below its own
    average" (the mean still dominated by the higher pre-action prices)
    for months afterward, which can flip the whole cycle's stop-width
    call since several logged cycles sit within a few points of the 45%
    threshold. Uses the legacy heuristic (no symbol/dates/classifier) --
    this runs across the whole ~200-symbol universe every cycle, so the
    same cost concern that keeps CORP_ACTION_GREY_ZONE_ENABLED off
    applies here too; the hard band alone already catches every real
    split, same as the selection signal's own fallback when the
    classifier path isn't given symbol/dates.
    """
    days = [d for d in sorted(price_hist) if d <= as_of]
    above = []
    for s in symbols:
        cl = [float(price_hist[d].at[s, "close_price"]) for d in days
              if s in price_hist[d].index
              and pd.notna(price_hist[d].at[s, "close_price"])
              and price_hist[d].at[s, "close_price"] > 0]
        if len(cl) < 200:
            continue
        cl = split_adjust(cl)
        above.append(1 if cl[-1] > pd.Series(cl).tail(200).mean() else 0)
    if not above:
        logger.warning("Breadth unavailable (no symbol had 200 sessions)")
        return float("nan")
    return 100.0 * float(np.mean(above))


def resolve_stop_pct(as_of: date, symbols=None, price_hist: dict = None) -> float:
    """
    The stop width for the cycle beginning after `as_of`.

    Falls back to the fixed V4_STOP_LOSS_PCT when the regime rule is
    disabled or breadth cannot be computed -- a missing regime signal
    must never leave a position without a stop.
    """
    if not getattr(config, "REGIME_STOP_ENABLED", False):
        return config.V4_STOP_LOSS_PCT
    if price_hist is None or symbols is None:
        return config.V4_STOP_LOSS_PCT
    b = market_breadth(as_of, symbols, price_hist)
    if b != b:
        logger.warning("Breadth NaN; using fixed %.1f%% stop", config.V4_STOP_LOSS_PCT)
        return config.V4_STOP_LOSS_PCT
    wide = b < config.REGIME_BREADTH_THRESHOLD
    stop = config.REGIME_STOP_WIDE_PCT if wide else config.REGIME_STOP_TIGHT_PCT
    logger.info("Breadth %.1f%% (threshold %.1f) -> %s market -> %.0f%% stop",
                b, config.REGIME_BREADTH_THRESHOLD,
                "beaten-down" if wide else "healthy", stop)
    return stop


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

def _explain_breach(symbol, day, prev_close, close, observed, hard):
    """
    Decide the adjustment ratio for one suspicious day-on-day move.

    Returns (ratio, source). ratio 1.0 means "leave it alone".

    The classifier reads NSE's actual corporate-action filings, so it can
    tell a 5:4 bonus from a bad day -- something a fixed band cannot. But
    it can be disabled, rate-limited or wrong, and it must never make the
    guard WEAKER than the band alone: if it cannot answer and the move is
    outside the hard band, we still adjust, exactly as before.
    """
    try:
        import corporate_actions
        verdict = corporate_actions.classify(symbol, day, prev_close, close)
    except Exception as exc:                     # never abort a run
        logger.warning("Corporate-action classifier unavailable for %s %s: %s",
                       symbol, day, exc)
        verdict = None

    cls, ratio = None, 1.0
    if verdict:
        cls = verdict.get("classification")
        try:
            ratio = float(verdict.get("adjustment_ratio") or 1.0)
        except (TypeError, ValueError):
            ratio = 1.0

    if hard:
        # Outside the hard band the move is not physically available to an
        # F&O stock: the dynamic price band is +/-10% and flexes to ~20%,
        # so a -50% day is a corporate action, full stop. The classifier
        # may REFINE the ratio here; it may NOT veto the adjustment. A
        # thin or stale filings feed otherwise returns GENUINE_MOVE and we
        # book a fake -50% loss -- exactly the BSE failure, reintroduced.
        if cls not in (None, "UNKNOWN", "GENUINE_MOVE") and ratio > 0 \
                and abs(ratio - 1.0) > 1e-9:
            return ratio, f"classifier:{cls}"
        if cls == "GENUINE_MOVE":
            logger.warning(
                "%s %s: classifier called the %.1f%% move GENUINE, but it is "
                "outside the hard band -- adjusting anyway", symbol, day,
                (observed - 1) * 100)
        return observed, "band"

    # Grey zone: plausible as a real move AND as a small bonus (a 5:4 is
    # -20%). Only the filings can separate them, so adjust ONLY on a
    # positive identification. Silence means leave it alone.
    if cls not in (None, "UNKNOWN", "GENUINE_MOVE") and ratio > 0 \
            and abs(ratio - 1.0) > 1e-9:
        logger.info("%s %s: %.1f%% move identified as %s, adjusting",
                    symbol, day, (observed - 1) * 100, cls)
        return ratio, f"classifier:{cls}"
    return 1.0, "none"


def adjust_holding_window(price_by_date, hold_dates, symbols=None,
                          low=0.72, high=1.40, back_adjust=False,
                          grey_low=0.85, grey_high=1.18, use_classifier=None,
                          classify_symbols=None, return_factors=False):
    """
    Neutralise unadjusted corporate actions across the HOLDING window.

    `split_adjust` only cleans the volatility lookback. The prices a
    position is actually walked against -- the OHLC read in
    simulate_month -- were never adjusted, so a split read as a crash and
    tripped the stop. BSE's 2:1 on 23-05-2025 booked a -61.99% "loss" and
    cost the Apr-2025 cycle 6.2 percentage points. Live, the same event
    sends a spurious EXIT alert on a stock that merely split.

    A broker adjusts the resting stop on the ex-date, so the correct
    modelling is for the split to be a non-event. Every bar from the
    ex-date onward is rescaled by the implied factor.

    Returns the original dict untouched when nothing breaches, which is
    the overwhelmingly common case, so the cost is one close-series scan.
    Only breaching symbols on affected dates are copied.

    `return_factors=True` additionally returns {symbol: final_factor} --
    the cumulative price multiplier this call applied to that symbol by
    the LAST date in the window (1.0 for anything untouched). A caller
    that tracks whole share counts OUTSIDE this function's own adjusted
    price series (research/carry_forward_v5.py's book ledger -- real
    money, real tradeable shares) needs this: `factor` is exactly the
    ratio a real share count must be multiplied by across the same
    action to stay value-consistent with RAW, unadjusted market prices
    (a 3:1 split triples the factor here and must triple the share
    count there). Added 14-Aug-2026 -- found via BSE's 23-May-2025 split
    producing a ~3x-overstated whole-share valuation in that ledger.
    """
    # OFF by default, deliberately. The hard band needs no network and is
    # what protects against a split; the classifier only adds precision in
    # the grey zone. Enabling it for every symbol in the universe turned a
    # 3-second daily report into a timeout, because each grey-zone breach
    # is an NSE fetch plus an LLM call. Turn it on for a specific symbol
    # list, or set CORP_ACTION_GREY_ZONE_ENABLED once the cost is capped.
    if use_classifier is None:
        use_classifier = bool(getattr(config, "CORP_ACTION_GREY_ZONE_ENABLED",
                                      False))

    dates = [d for d in hold_dates if d in price_by_date]
    if len(dates) < 2:
        return (price_by_date, {}) if return_factors else price_by_date

    syms = symbols
    if syms is None:
        first = price_by_date[dates[0]]
        syms = list(first.index)

    factors = {}
    for sym in syms:
        prev, fac = None, 1.0
        per_date = {}
        for d in dates:
            frame = price_by_date[d]
            if sym not in frame.index:
                continue
            c = frame.at[sym, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            c = float(c)
            adj_prev = prev
            if adj_prev is not None:
                r = (c * fac) / adj_prev
                hard = (r < low or r > high)
                grey = (r < grey_low or r > grey_high)
                if grey:
                    # Only names that can actually affect P&L are worth an
                    # NSE fetch and an LLM call. Everything else falls back
                    # to the band, which needs no network.
                    may_classify = use_classifier and (
                        classify_symbols is None or sym in classify_symbols)
                    if may_classify:
                        ratio, _src = _explain_breach(
                            sym, d, adj_prev, c * fac, r, hard)
                    else:
                        ratio = r if hard else 1.0
                    if abs(ratio - 1.0) > 1e-9:
                        # Divide out the ACTION ratio, not the observed
                        # move. A 1:2 split on a day the stock also fell
                        # 10% shows observed 0.45; scaling by 1/0.45 would
                        # erase the real -10% along with the split. The
                        # classifier's 0.5 keeps it. When we fall back to
                        # the band, ratio IS the observed move, so this
                        # reduces to the previous behaviour exactly.
                        fac *= 1.0 / ratio
            # store EVERY date, including the 1.0s before the first action:
            # back-adjustment divides the whole series by the final factor,
            # so the pre-action days are exactly the ones that must move.
            per_date[d] = fac
            prev = c * fac
        if any(f != 1.0 for f in per_date.values()):
            factors[sym] = per_date

    if not factors:
        return (price_by_date, {}) if return_factors else price_by_date

    # Snapshot BEFORE the back_adjust/filtering branches below reshape
    # `factors` -- this is the true cumulative multiplier applied to each
    # touched symbol over the whole window, independent of which basis
    # the returned PRICE series ends up rebased to.
    final_factors = {sym: per_date.get(dates[-1], 1.0)
                     for sym, per_date in factors.items()}
    final_factors = {s: f for s, f in final_factors.items() if f != 1.0}

    if back_adjust:
        # Forward-scaling restates everything onto the OLDEST basis, right
        # for walking a position (the entry price is old) but wrong for
        # features: BAJFINANCE would read Rs11,352 when it trades at
        # Rs1,141. Dividing by the factor in force on the last day leaves
        # recent prices untouched and restates history instead.
        rebased = {}
        for sym, per_date in factors.items():
            last = per_date.get(dates[-1], 1.0) or 1.0
            scaled = {d: f / last for d, f in per_date.items() if f / last != 1.0}
            if scaled:
                rebased[sym] = scaled
        factors = rebased
        if not factors:
            return (price_by_date, final_factors) if return_factors else price_by_date
    else:
        factors = {s: {d: f for d, f in p.items() if f != 1.0}
                   for s, p in factors.items()}
        factors = {s: p for s, p in factors.items() if p}
        if not factors:
            return (price_by_date, final_factors) if return_factors else price_by_date

    logger.warning("Corporate-action adjustment applied over the holding "
                   "window for: %s", ", ".join(sorted(factors)))
    out = dict(price_by_date)
    touched = sorted({d for pd_ in factors.values() for d in pd_})
    cols = ("open_price", "high_price", "low_price", "close_price")
    for d in touched:
        frame = out[d].copy()
        # An integer price column rejects a scaled float in-place; pandas
        # currently warns and will raise in a future version.
        for col in cols:
            if col in frame.columns and frame[col].dtype.kind in "iu":
                frame[col] = frame[col].astype(float)
        for sym, per_date in factors.items():
            f = per_date.get(d)
            if not f or sym not in frame.index:
                continue
            for col in cols:
                if col in frame.columns and pd.notna(frame.at[sym, col]):
                    frame.at[sym, col] = float(frame.at[sym, col]) * f
        out[d] = frame
    return (out, final_factors) if return_factors else out


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
        closes, vols, vals, close_dates = [], [], [], []
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
            tv = frame.at[sym, "turnover"] if "turnover" in frame.columns else np.nan
            if pd.notna(tv):
                vals.append(float(tv))
        if len(closes) < config.V4_VOL_LOOKBACK_DAYS - 10:
            continue
        closes = split_adjust(closes, symbol=sym, dates=close_dates,
                              flagged=flagged)
        s = pd.Series(closes)
        rets = s.pct_change().dropna().tail(config.V4_VOL_LOOKBACK_DAYS)
        turnover_cr = None
        if len(vals) >= 5:
            turnover_cr = float(pd.Series(vals).tail(
                config.TURNOVER_LOOKBACK_DAYS).median()) / 1e7
        rows.append({
            "symbol": sym,
            "close": closes[-1],
            "turnover_cr": turnover_cr,
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

    # Liquidity floor. Thin and volatile is where a 5% market-order stop
    # fills worst, and this strategy deliberately selects volatile names.
    if "turnover_cr" in df.columns:
        thin = df["turnover_cr"].notna() & (df["turnover_cr"] < config.MIN_TURNOVER_CRORE)
        if thin.any():
            logger.info("Liquidity floor dropped %d name(s) below Rs %.0f cr median turnover",
                        int(thin.sum()), config.MIN_TURNOVER_CRORE)
            df = df[~thin]
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
    last: float = None

    @property
    def pnl_pct(self) -> float:
        if not self.entry or self.last is None:
            return 0.0
        return (self.last - self.entry) / self.entry * 100

    @property
    def pct_to_target(self) -> float:
        if not self.last:
            return 0.0
        return (self.target - self.last) / self.last * 100

    @property
    def target_placeable(self) -> bool:
        """
        True once a sell limit at `target` is inside the exchange's dynamic
        price band. Below this the order is rejected at entry, which is why
        the target is not placed on entry day.
        """
        if not self.last:
            return False
        return self.target <= self.last * (1 + config.PRICE_BAND_PCT / 100.0)


@dataclass
class BasketDecision:
    """What `basket_for` decided, with the working shown."""
    expiry: date
    symbols: list                    # the ten to hold, veto applied
    ranked_order: list               # full ranking, vetoed names removed
    table: object                    # rank_universe DataFrame (pre-veto)
    full: object                     # full scored frame
    hist: dict
    veto_dropped: list               # [(symbol, reason)]
    veto_added: list                 # backfilled replacements
    veto_ran: bool                   # False when the ASM feed failed
    stop_pct: float


@dataclass
class Exit:
    symbol: str
    entry: float
    exit_px: float
    reason: str          # STOP | TARGET | ROLLOVER | OFF_MOMENTUM
    exit_date: date

    @property
    def pnl_pct(self) -> float:
        return (self.exit_px - self.entry) / self.entry * 100


@dataclass
class MonthResult:
    month: str
    entry_date: date
    exit_date: date
    return_pct: float
    trades: int
    slots: list = field(default_factory=list)
    carry: dict = field(default_factory=dict)
    # Rich detail so daily_report does not need a second simulator.
    open_positions: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    to_buy: list = field(default_factory=list)
    empty_slots: int = 0
    # {symbol: cumulative_price_factor} for any split/bonus detected over
    # this call's hold_dates (see adjust_holding_window's return_factors).
    # A real, whole-share ledger tracking share counts OUTSIDE this
    # function's own adjusted price series (e.g.
    # research/carry_forward_v5.py's book, or a live brokerage book) must
    # multiply its share count for that symbol by this same factor to
    # stay value-consistent -- this function only adjusts PRICES, it
    # never touches a share count because it doesn't track one.
    corp_action_factors: dict = field(default_factory=dict)


def basket_for(expiry: date, symbols=None, sector_map=None, session=None,
               price_hist=None):
    """
    THE answer to "what ten names for this expiry". Use this everywhere.

    Returns a BasketDecision. `ranked_order` already has vetoed names
    removed, which matters because simulate_month fills its slots by
    walking that list -- passing the vetoed set as `basket_symbols` alone
    does nothing.

    WHY THIS EXISTS
      Three call sites used to derive the basket independently:
      daily_report.build, daily_report.build_entry_sheet and
      run_strategy.cmd_basket. Only two applied the surveillance veto, so
      on 03-Aug-2026 the evening note tracked KALYANKJIL (ASM Stage I,
      vetoed out of the basket actually sent) while the real book held
      ADANIGREEN, and `run_strategy.py basket` printed a third answer
      again. Same disease as the two expiry resolvers fixed that morning:
      one concept, several implementations, and the wrong one live.
    """
    import scoring                                          # noqa: E402
    import nse_client                                       # noqa: E402

    symbols = symbols if symbols is not None else load_fo_universe()
    sector_map = sector_map if sector_map is not None else load_sector_map()
    hist = price_hist if price_hist is not None else load_price_history(expiry, symbols)
    if expiry not in hist:
        raise StrategyError(f"No bhavcopy for expiry {expiry}")

    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(expiry))
    signals = compute_signals_cached(hist, fo, expiry, symbols)
    basket, full = rank_universe(signals, sector_map)

    picks = basket["symbol"].tolist()
    ranked = list(full.index)
    dropped, added, veto_ran = [], [], False

    if getattr(config, "VETO_ENABLED", False):
        try:
            import surveillance
            kept, dropped, added, veto_ran = surveillance.apply_veto(
                basket, ranked, sector_map, session)
            if veto_ran and kept:
                picks = kept
                blocked = {s for s, _why in dropped}
                ranked = kept + [s for s in ranked
                                 if s not in kept and s not in blocked]
        except Exception as exc:                    # never break a run
            logger.error("Veto step failed, continuing without it: %s", exc)
            veto_ran = False

    stop_pct = resolve_stop_pct(expiry, symbols, hist)

    # Rebuild the display table for the FINAL ten. A backfilled name is not
    # in rank_universe's output -- it comes from the full ranking -- so
    # filtering the original table silently returns nine rows.
    table = full.loc[[s for s in picks if s in full.index]].reset_index()
    if "symbol" not in table.columns and "index" in table.columns:
        table = table.rename(columns={"index": "symbol"})
    table.insert(0, "rank", range(1, len(table) + 1))
    table["weight_pct"] = round(100 / max(len(picks), 1), 2)
    table["stop_loss"] = (table["close"] * (1 - stop_pct / 100)).round(2)
    table["target"] = (table["close"] * (1 + config.V4_TARGET_PCT / 100)).round(2)

    return BasketDecision(expiry=expiry, symbols=picks, ranked_order=ranked,
                          table=table, full=full, hist=hist,
                          veto_dropped=dropped, veto_added=added,
                          veto_ran=veto_ran, stop_pct=stop_pct)


def simulate_month(ranked_order, price_by_date, hold_dates, sector_map,
                   reentry_policy=None, top_n=None,
                   stop_pct=None, target_pct=None,
                   carry_in: dict = None, basket_symbols=None,
                   carry_forward: bool = None,
                   candidate_fn=None,
                   use_classifier: bool = None,
                   entry_overrides: dict = None,
                   ratchet_trigger_pct: float = None,
                   ratchet_lock_pct: float = None) -> MonthResult:
    """
    Day-by-day simulation of `top_n` equally weighted slots.

    `entry_overrides` ({symbol: (price, date, risk_anchor) | None})
    replaces the default "fills at hold_dates[0]'s open" assumption for
    this month's INITIAL entries -- used to backtest a realistic
    multi-day limit-then-market fill chain (day-1 limit, day-2 requote,
    day-3 forced market) instead of perfect same-day execution.
    `risk_anchor` decouples stop/target from wherever the fill actually
    happened -- see open_position's docstring for why. A value of None
    means the entry was deliberately ABORTED (gap risk already breached
    the anchor stop before a fill was reached) and the slot stays in
    cash for the month, no fallback fill. A symbol absent from the dict
    still fills the old way. Every existing caller passes nothing and
    gets identical behaviour to before this parameter existed.

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

    RATCHET (`ratchet_trigger_pct`, `ratchet_lock_pct`), both None by
    default -- every existing caller is unaffected. When set, the ORIGINAL
    stop_pct/target_pct are left exactly as they are (no conviction tiers,
    no promotion gate -- see the 03-Aug-2026 ATR-SOP experiment in
    CONTEXT.md for why that combined design is not reused here). The only
    change: once a session's HIGH reaches entry*(1+ratchet_trigger_pct/100),
    the stop is raised (never lowered) to entry*(1+ratchet_lock_pct/100)
    for every subsequent session. The raise takes effect starting the
    NEXT session, not the one that triggered it -- the low that would
    check today's OLD stop and the high that triggers the ratchet are
    both intraday and unordered, so applying the new stop retroactively
    on the same day would be look-ahead, the same reasoning the STOP/
    TARGET gap-fill comment above already applies to entry order.

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

    # Splits and bonuses are not a P&L event -- the broker adjusts the
    # resting stop on the ex-date. Neutralise them before walking prices.
    # The classifier is consulted ONLY for names that can actually be
    # held: this month's basket plus anything carried in. Letting it loose
    # on all ~206 universe symbols fired one NSE fetch and one LLM call
    # per grey-zone move and timed the daily report out.
    #
    # `use_classifier` is a CALLER decision, not a global. The live report
    # wants it: a bonus that the band misses would otherwise fire a wrong
    # stop. A backtest wants it OFF -- it makes results depend on what a
    # model answers, so two runs of the same backtest can disagree, and it
    # turned one cycle from instant into 23.7s. Backtests must be
    # deterministic and offline; research/harness.py passes False.
    # Limit BOTH the band scan and the classifier to names that can be
    # held. Previously only the classifier was limited, so the band still
    # swept all ~208 universe symbols and warned about corporate actions
    # in stocks we do not own (IVZINNIFTY, NARMADA on 03-Aug-2026) --
    # wasted work, and a warning that trains you to ignore warnings.
    _held = set(basket_symbols or []) | set((carry_in or {}).keys())
    price_by_date, corp_action_factors = adjust_holding_window(
        price_by_date, hold_dates,
        symbols=sorted(_held) or None,
        classify_symbols=_held or None,
        use_classifier=use_classifier,
        return_factors=True)

    held = {i: None for i in range(top_n)}
    sector_count, banned, pending = {}, set(), []
    pnl = [0.0] * top_n
    chains = [[] for _ in range(top_n)]
    exits = []
    trades = 0

    def sector_of(sym):
        return (sector_map or {}).get(sym, f"Unclassified:{sym}")

    def next_candidate(queued):
        if not getattr(config, "V4_REDEPLOY_ENABLED", True):
            return None          # freed slots hold cash to expiry
        """
        Pick the replacement for a freed slot.

        `candidate_fn(eligible)` -> symbol or None lets the live path
        substitute a daily, cash-only judgement for the frozen expiry
        ordering. The BACKTEST deliberately passes nothing: a model whose
        training data covers the test period cannot be backtested, so the
        historical path stays mechanical and reproducible.

        Returning None is legitimate -- it means nothing cleared the
        deployment hurdle and the slot stays in cash.
        """
        eligible = [c for c in ranked_order
                    if available(c) and c not in queued]
        if not eligible:
            return None
        if candidate_fn is None:
            return eligible[0]
        try:
            return candidate_fn(eligible)
        except Exception as exc:
            logger.error("candidate_fn failed (%s); using top-ranked %s",
                         exc, eligible[0])
            return eligible[0]

    def available(sym):
        if sym in banned:
            return False
        if any(p and p.symbol == sym for p in held.values()):
            return False
        if sector_map and sector_count.get(sector_of(sym), 0) >= max_per_sector:
            return False
        return True

    def open_position(slot, sym, day, basis=None, basis_date=None, risk_anchor=None):
        """
        `risk_anchor`, if given, is the price stop/target are computed
        from -- separate from `basis`, the actual cost basis P&L is
        measured against. Per Perold's arrival-price principle: risk
        parameters should be pinned to the price at the moment the
        decision became actionable, not to wherever a delayed fill
        actually happened, or a slow fill inherits a stop that's crept
        upward (closer to the market) purely as an artefact of the
        entry being late -- exactly what caused the PNBHOUSING/BSE
        blowups in the fill-realism backtest.
        """
        if basis is not None:
            anchor = risk_anchor if risk_anchor is not None else basis
            held[slot] = Position(sym, float(basis),
                                  float(anchor) * (1 - stop_pct / 100),
                                  float(anchor) * (1 + target_pct / 100),
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
    # A pool of unclaimed slot indices, not a monotonic counter. FIXED
    # 13-Aug-2026: the old counter treated "assigned a slot number" as
    # permanent even for a ROLLOVER sell, whose slot is never actually
    # occupied (held[slot] is never set -- redeployment is off by
    # default, so nothing fills it). That silently starved the
    # entry_overrides fill loop below of slot numbers in any month with
    # enough simultaneous rollovers: 2 holds + 4 rollovers consumed 6 of
    # 10 slot numbers before a single fresh buy was attempted, so the
    # last 4 of 8 needed buys were dropped with no exit, no position, no
    # error -- found backtesting the carry-forward HOLD-rebalance
    # mechanism (research/carry_forward_v5.py), where it silently erased
    # ~4 slots' worth of capital from one month's NAV.
    free_slots = list(range(top_n))

    if carry_forward and carry_in:
        # Re-slot carried positions that are still in this month's basket --
        # no trade, no new cost basis.
        for sym, pos in carry_in.items():
            if not free_slots:
                break
            if sym in basket_set and available(sym):
                slot = free_slots.pop(0)
                open_position(slot, sym, first, basis=pos.entry, basis_date=pos.entry_date)

        # Everything else that was open is sold at today's open (a real
        # trade, tagged ROLLOVER so it's distinguishable from STOP/TARGET).
        # held[] is never set for this slot, so it goes straight back to
        # free_slots -- reusable by THIS month's fill loop below, not just
        # a future redeploy -- unless a same-month redeploy candidate
        # actually claims it (only possible when V4_REDEPLOY_ENABLED).
        for sym, pos in carry_in.items():
            if sym in basket_set:
                continue
            if frame0 is None or sym not in frame0.index:
                continue
            px_open = frame0.at[sym, "open_price"]
            if pd.isna(px_open) or px_open <= 0:
                continue
            use_slot = free_slots.pop(0) if free_slots else 0
            ret = (float(px_open) - pos.entry) / pos.entry * 100
            pnl[use_slot] += ret
            trades += 1
            chains[use_slot].append((sym, round(ret, 2), "ROLLOVER", str(first)))
            exits.append(Exit(sym, pos.entry, float(px_open), "ROLLOVER", first))
            if policy != "always":
                banned.add(sym)
            if use_slot not in free_slots:
                free_slots.insert(0, use_slot)
            cand = next_candidate([s for _, s in pending])
            if cand:
                pending.append((use_slot, cand))
                if use_slot in free_slots:
                    free_slots.remove(use_slot)

    # Fill whatever slots are still empty, same as a no-carry month.
    # A symbol with an entry_overrides date later than `first` isn't
    # opened yet -- it's queued in `deferred` so the day-by-day loop
    # below opens it (at its REAL fill price) on the day it actually
    # filled, and stop/target checks correctly skip it until then
    # (held[slot] stays None, same as any other empty slot).
    #
    # entry_overrides[sym] can be:
    #   (price, date, risk_anchor)  -- fills at `price` on `date`, stop/
    #                                  target computed off `risk_anchor`
    #   None                        -- ABORTED: gap risk breached the
    #                                  anchor stop before a fill was
    #                                  ever reached, so the slot is
    #                                  deliberately left empty (cash)
    #                                  for the rest of the month, never
    #                                  falling back to an auto-fill.
    #   absent from the dict        -- no override; normal auto-fill at
    #                                  `first`'s open, as before.
    deferred = {}
    has_overrides = entry_overrides is not None
    for sym in ranked_order:
        if not free_slots:
            break
        if not available(sym):
            continue
        if has_overrides and sym in entry_overrides:
            override = entry_overrides[sym]
            if override is None:
                continue          # aborted -- slot stays empty, no backfill
            px, dte, anchor = override
            slot = free_slots.pop(0)
            if dte == first:
                if not open_position(slot, sym, first, basis=px, basis_date=first,
                                     risk_anchor=anchor):
                    free_slots.insert(0, slot)
            else:
                deferred.setdefault(dte, []).append((slot, sym, px, anchor))
                sector_count[sector_of(sym)] = sector_count.get(sector_of(sym), 0) + 1
        else:
            slot = free_slots.pop(0)
            if not open_position(slot, sym, first):
                free_slots.insert(0, slot)

    for i in range(1, len(hold_dates)):
        day = hold_dates[i]
        for slot_id, sym in pending:
            if available(sym):
                open_position(slot_id, sym, day)
        pending = []
        for slot_id, sym, px, anchor in deferred.pop(day, []):
            open_position(slot_id, sym, day, basis=px, basis_date=day, risk_anchor=anchor)

        frame = price_by_date.get(day)
        if frame is None:
            continue
        for slot_id in range(top_n):
            pos = held.get(slot_id)
            if pos is None or pos.symbol not in frame.index:
                continue
            low = frame.at[pos.symbol, "low_price"]
            high = frame.at[pos.symbol, "high_price"]
            opn = frame.at[pos.symbol, "open_price"] \
                if "open_price" in frame.columns else None
            exit_px, reason = None, None
            # A resting order fills AT its price when the level trades --
            # but a session that GAPS through it fills at the open, which
            # is worse than the stop and better than the target. That gap
            # is the only way a stop loses more than its width: TRENT
            # closed 3343.80 on 06-07-2026 and opened 3080.00 against a
            # 3120.75 stop, so the fill was -6.24%, not -5.00%.
            if pd.notna(low) and low <= pos.stop:
                exit_px, reason = pos.stop, "STOP"
                if opn is not None and pd.notna(opn) and float(opn) < pos.stop:
                    exit_px = float(opn)
            elif pd.notna(high) and high >= pos.target:
                exit_px, reason = pos.target, "TARGET"
                if opn is not None and pd.notna(opn) and float(opn) > pos.target:
                    exit_px = float(opn)
            if exit_px is None:
                if (ratchet_trigger_pct is not None and pd.notna(high)
                        and float(high) >= pos.entry * (1 + ratchet_trigger_pct / 100)):
                    new_stop = pos.entry * (1 + ratchet_lock_pct / 100)
                    if new_stop > pos.stop:
                        pos.stop = new_stop
                continue

            ret = (exit_px - pos.entry) / pos.entry * 100
            pnl[slot_id] += ret
            trades += 1
            chains[slot_id].append((pos.symbol, round(ret, 2), reason, str(day)))
            exits.append(Exit(pos.symbol, pos.entry, float(exit_px), reason, day))
            sector_count[sector_of(pos.symbol)] -= 1
            if policy == "never" or (policy == "not_if_stopped" and reason == "STOP"):
                banned.add(pos.symbol)
            held[slot_id] = None

            if i < len(hold_dates) - 1:
                cand = next_candidate([s for _, s in pending])
                if cand:
                    pending.append((slot_id, cand))

    final = hold_dates[-1]
    frame = price_by_date.get(final)

    # Snapshot the open book with ORIGINAL cost bases, before the
    # carry-forward branch below re-marks them to the closing price.
    open_positions = []
    for slot_id in range(top_n):
        pos = held.get(slot_id)
        if pos is None:
            continue
        if frame is not None and pos.symbol in frame.index:
            c = frame.at[pos.symbol, "close_price"]
            if pd.notna(c) and c > 0:
                pos.last = float(c)
        open_positions.append(pos)

    to_buy = [sym for _, sym in pending]
    carry_out = {}
    if carry_forward:
        # Mark still-open positions to `final`'s OPEN, not its close.
        # `final` (hold_dates[-1]) IS next month's own Day-1 -- the same
        # calendar day a fresh buy there anchors to that day's open
        # (Perold arrival-price rule, see open_position's docstring). A
        # carried hold marked to the CLOSE instead (an earlier version's
        # behaviour, found 14-Aug-2026 when a HOLD's refreshed entry
        # didn't line up with what a fresh buy that same day would have
        # used) sits its stop/target one session's worth of drift ahead
        # of every fresh buy's, for no reason -- same day, two different
        # reference clocks. Nothing is actually sold here, so this is not
        # a trade and does not touch `trades`. The resulting basis is
        # handed back so next month's call can decide HOLD vs SELL
        # against the new basket.
        for slot_id in range(top_n):
            pos = held.get(slot_id)
            if pos is None or frame is None or pos.symbol not in frame.index:
                continue
            px_open = frame.at[pos.symbol, "open_price"]
            if pd.isna(px_open) or px_open <= 0:
                continue
            ret = (float(px_open) - pos.entry) / pos.entry * 100
            pnl[slot_id] += ret
            chains[slot_id].append((pos.symbol, round(ret, 2), "MARK", str(final)))
            carry_out[pos.symbol] = Position(
                pos.symbol, float(px_open),
                float(px_open) * (1 - stop_pct / 100),
                float(px_open) * (1 + target_pct / 100), final)
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
        open_positions=open_positions,
        exits=exits,
        to_buy=to_buy,
        empty_slots=top_n - len(open_positions) - len(to_buy),
        corp_action_factors=corp_action_factors,
    )
