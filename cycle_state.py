"""
Incremental cycle state: today = yesterday + one bhavcopy.

WHY
---
`daily_report.build` re-derived the basket every evening, which meant
loading 260 days of history for all 208 symbols and re-running the
ranking, breadth and surveillance veto -- to recompute an answer that was
fixed on expiry day and cannot change until the next one. That nightly
recomputation is where two of the 03-Aug-2026 bugs lived.

Everything expensive happens ONCE, on expiry day, in `open_cycle`. After
that each evening reads the stored state, applies exactly one day's
bhavcopy, and writes it back.

WHAT THE STATE MUST CARRY, and why
----------------------------------
entry / stop / target   frozen on entry day; never recomputed
last_close              a corporate action is detected from
                        today's close / yesterday's close, so yesterday's
                        close has to survive in the state
status                  HOLD or EXITED. EXITED is terminal: the exit is
                        recorded once and reported at its realised price
                        forever after, so a stop-out is not re-announced
                        every evening for the rest of the cycle
stale                   set when a symbol is missing from a bhavcopy
                        (suspension, F&O ban, NSE omission). The previous
                        close is carried and the stop is NOT evaluated
                        against absent data
as_of                   the last session already applied, so the next run
                        can tell how far behind it is

STALENESS
---------
`advance` does NOT assume it is one day behind. If the machine was off,
or the task fired before the bhavcopy published, the state can be several
sessions stale -- and a stop that triggered on a skipped day would
otherwise never be seen, leaving the note saying HOLD on a position that
is gone. It replays every missing session in order.
"""
import json
import logging
import os
from datetime import date, timedelta

import pandas as pd

import config
import nse_client
import scoring
import strategy

logger = logging.getLogger("momentum_tracker.cycle_state")

STATE_FILE = os.path.join(config.DATA_DIR, "cycle_state.json")

_OHLC = {"OpnPric": "open_price", "HghPric": "high_price",
         "LwPric": "low_price"}


# ---------------------------------------------------------------------------
# one day's prices
# ---------------------------------------------------------------------------

def frame_for(day: date):
    """
    One session's OHLC, indexed by symbol. None when NSE published nothing
    (weekend, holiday, or the file is not out yet).
    """
    if day.weekday() >= 5:
        return None
    try:
        raw = nse_client.fetch_cm_bhavcopy(day)
    except nse_client.NseFetchError as exc:
        logger.debug("No CM bhavcopy for %s (%s)", day, exc)
        return None
    norm = scoring.normalize_cm_columns(raw)
    raw_ohlc = raw.drop_duplicates(subset=["TckrSymb"], keep="first")
    for src, dst in _OHLC.items():
        if src in raw_ohlc.columns and dst not in norm.columns:
            norm[dst] = (raw_ohlc.set_index("TckrSymb")[src]
                         .reindex(norm["symbol"]).values)
    return norm.set_index("symbol")


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def load(path: str = None) -> dict:
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
    os.replace(tmp, path)          # atomic: a crash mid-write cannot corrupt


# ---------------------------------------------------------------------------
# expiry day -- the one expensive run
# ---------------------------------------------------------------------------

def open_cycle(expiry: date, session=None, path: str = None) -> dict:
    """
    Rank, veto, and record the opening state. Entry is the OPEN of the
    first session after `expiry`; if that session has not happened yet the
    positions are stored with entry=None and filled in by the first
    `advance`.
    """
    decision = strategy.basket_for(expiry, session=session)
    td = strategy.known_trading_days()
    nxt_y, nxt_m = ((expiry.year + 1, 1) if expiry.month == 12
                    else (expiry.year, expiry.month + 1))
    try:
        next_expiry = strategy.expiry_for(nxt_y, nxt_m, trading_days=td)
    except strategy.StrategyError:
        next_expiry = strategy.expiry_for(nxt_y, nxt_m)

    state = {
        "expiry": str(expiry),
        "next_expiry": str(next_expiry),
        "entry_date": None,
        "as_of": str(expiry),
        "stop_pct": decision.stop_pct,
        "target_pct": config.V4_TARGET_PCT,
        "slots": config.PORTFOLIO_SIZE,
        "veto_dropped": [[s, w] for s, w in decision.veto_dropped],
        "positions": {sym: {"symbol": sym, "entry": None, "entry_date": None,
                            "stop": None, "target": None, "status": "PENDING",
                            "last_close": None, "exit_px": None,
                            "exit_date": None, "reason": None, "stale": False}
                      for sym in decision.symbols},
    }
    save(state, path)
    logger.info("Opened cycle %s -> %s with %d names (stop %.0f%%)",
                expiry, next_expiry, len(state["positions"]), decision.stop_pct)
    return state


# ---------------------------------------------------------------------------
# one session
# ---------------------------------------------------------------------------

def _apply_corporate_action(pos, close, day, use_classifier):
    """
    Restate the WHOLE position when a split or bonus lands.

    Adjusting only the day's price is not enough: entry, stop and target
    are all quoted in pre-action rupees, so after a 1:2 the stop would sit
    at double the traded price and read as breached every day thereafter.
    """
    prev = pos.get("last_close")
    if not prev or prev <= 0 or not close or close <= 0:
        return 1.0
    r = close / prev
    if config.V4_SPLIT_RATIO_LOW <= r <= config.V4_SPLIT_RATIO_HIGH:
        return 1.0

    hard = True
    ratio = r
    if use_classifier:
        ratio, _src = strategy._explain_breach(pos["symbol"], day, prev,
                                               close, r, hard)
    if abs(ratio - 1.0) <= 1e-9:
        return 1.0

    for k in ("entry", "stop", "target", "last_close"):
        if pos.get(k):
            pos[k] = float(pos[k]) * ratio
    logger.warning("%s: corporate action on %s, ratio %.4f -- entry/stop/"
                   "target restated to %.2f/%.2f/%.2f", pos["symbol"], day,
                   ratio, pos["entry"] or 0, pos["stop"] or 0, pos["target"] or 0)
    return ratio


def apply_session(state: dict, day: date, frame, use_classifier=None) -> dict:
    """Advance `state` by exactly one session. Mutates and returns it."""
    if use_classifier is None:
        use_classifier = bool(getattr(config, "CORP_ACTION_GREY_ZONE_ENABLED",
                                      False))
    stop_pct = state["stop_pct"] / 100.0
    target_pct = state["target_pct"] / 100.0

    for sym, pos in state["positions"].items():
        if pos["status"] == "EXITED":
            continue
        if frame is None or sym not in frame.index:
            pos["stale"] = True
            logger.warning("%s absent from the %s bhavcopy -- carrying the "
                           "previous close, stop not evaluated", sym, day)
            continue
        pos["stale"] = False
        row = frame.loc[sym]

        def val(col):
            v = row.get(col)
            return float(v) if pd.notna(v) else None

        o, h, l, c = (val("open_price"), val("high_price"),
                      val("low_price"), val("close_price"))
        if c is None or c <= 0:
            pos["stale"] = True
            continue
        o = o if o else c
        h = h if h else c
        l = l if l else c

        # entry day: fill at the open and freeze stop/target off it
        if pos["status"] == "PENDING":
            pos["entry"] = o
            pos["entry_date"] = str(day)
            pos["stop"] = o * (1 - stop_pct)
            pos["target"] = o * (1 + target_pct)
            pos["status"] = "HOLD"
            pos["last_close"] = c
            state["entry_date"] = state["entry_date"] or str(day)
            continue

        ratio = _apply_corporate_action(pos, c, day, use_classifier)
        if ratio != 1.0:
            o, h, l, c = (x * ratio for x in (o, h, l, c))

        # resting orders: fill AT the level, or at the open on a gap
        if l <= pos["stop"]:
            pos.update(status="EXITED", exit_px=min(o, pos["stop"]),
                       exit_date=str(day), reason="STOP")
        elif h >= pos["target"]:
            pos.update(status="EXITED", exit_px=max(o, pos["target"]),
                       exit_date=str(day), reason="TARGET")
        pos["last_close"] = c

    state["as_of"] = str(day)
    return state


def advance(state: dict, as_of: date, use_classifier=None) -> tuple:
    """
    Replay every session from the day after `state["as_of"]` up to `as_of`.

    Returns (state, applied) where `applied` is the list of dates actually
    processed -- normally one, more when the job has missed runs.
    """
    last = pd.to_datetime(state["as_of"]).date()
    applied = []
    d = last + timedelta(days=1)
    while d <= as_of:
        frame = frame_for(d)
        if frame is not None:
            apply_session(state, d, frame, use_classifier=use_classifier)
            applied.append(d)
        d += timedelta(days=1)
    if len(applied) > 1:
        logger.warning("State was %d sessions behind; replayed %s",
                       len(applied), ", ".join(str(x) for x in applied))
    return state, applied


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def summarise(state: dict) -> dict:
    """Per-position returns and the equal-weight cycle return."""
    slots = state.get("slots") or config.PORTFOLIO_SIZE
    holds, exits, total = [], [], 0.0
    for sym, pos in state["positions"].items():
        entry = pos.get("entry")
        if not entry:
            continue
        if pos["status"] == "EXITED":
            pct = (pos["exit_px"] / entry - 1) * 100
            exits.append({"symbol": sym, "entry": entry,
                          "exit_px": pos["exit_px"], "reason": pos["reason"],
                          "exit_date": pos["exit_date"], "pct": pct})
        else:
            last = pos.get("last_close") or entry
            pct = (last / entry - 1) * 100
            band = config.PRICE_BAND_PCT / 100.0
            holds.append({"symbol": sym, "entry": entry, "last": last,
                          "stop": pos["stop"], "target": pos["target"],
                          "pct": pct, "stale": pos.get("stale", False),
                          "target_placeable": pos["target"] <= last * (1 + band)})
        total += pct
    holds.sort(key=lambda r: -r["pct"])
    exits.sort(key=lambda r: r["exit_date"])
    return {"as_of": state["as_of"], "expiry": state["expiry"],
            "entry_date": state["entry_date"], "stop_pct": state["stop_pct"],
            "cycle_pct": total / slots if slots else 0.0,
            "holds": holds, "exits": exits,
            "cash_slots": slots - len(holds) - len(exits)}
