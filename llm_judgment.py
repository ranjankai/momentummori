"""
LLM judgment layer: per-stock target setting and daily exit judgement.

WHY THIS EXISTS
---------------
The flat +40% target is a blunt instrument. It fired twice in thirteen
months, and it cannot even be placed as a resting order until the stock
is up ~27.3% (F&O dynamic price band, +/-10% of previous close). A target
derived from the stock's own levels -- prior swing high, ATR, distance
above its moving averages -- lands where the stock might plausibly get
to, and usually sits close enough to be placeable on day one.

WHEN EACH RUNS
--------------
  TARGET  once, on entry day, per position. Recomputed only when a name
          carries into a new month, because the cost basis resets.
          NEVER recomputed intra-month -- a target that moves is not a
          target.
  EXIT    every day, mid-month only. Purely additive to the stop-loss: it
          can bring a position out early and can do nothing else.

Expiry day is mechanical. The composite rank decides what is held and
what is sold. No call is made here.

GROUNDING
---------
Every number the model sees is computed in this run and passed in the
prompt. It is never shown a chart, never asked what a level "usually" is,
and its `inputs_used` is validated against the payload -- a response
citing a field that was not supplied is rejected outright, because that
is the signature of an invented answer.

HARD LIMITS
-----------
config.LLM_TARGET_MAX_PCT is a ceiling, not a suggestion. The model may
propose any target up to it; anything higher is clamped and flagged.
Below LLM_TARGET_MIN_PCT the response is rejected and the flat target
applies. A failed or disabled layer always falls back to V4_TARGET_PCT,
so the strategy still runs.

BACKTESTING
-----------
This layer cannot be backtested honestly. The model's training data may
already contain the outcome of the period being tested, so a "judgement"
about March 2025 is leakage wearing a costume. Deploy forward only, and
log the flat-target counterfactual alongside so the two can be compared
after enough live months.
"""

import json
import logging
import os
from datetime import date

import numpy as np
import pandas as pd

import config
import llm

logger = logging.getLogger("momentum_tracker.llm_judgment")


TARGET_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "target_pct": {"type": "NUMBER"},
        "target_basis": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
        "inputs_used": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["target_pct", "target_basis", "confidence", "inputs_used"],
}

EXIT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "exit_now": {"type": "BOOLEAN"},
        "exit_reason": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
        "inputs_used": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["exit_now", "exit_reason", "confidence", "inputs_used"],
}


_EXIT_PROMPT = """\
You are an equity analyst reviewing ONE open position at the end of a
trading day. It sits in a ten-stock Indian momentum portfolio. It was
bought because it was among the strongest names in the universe. Your
only question:

  Is this stock STILL in momentum, or has it lost it?

If it has lost momentum, it is sold at tomorrow's open and the capital
goes elsewhere. If it is still running, it is left alone.

This is not a stop-loss decision. A {stop_pct:.0f}% stop already sits at the broker
and fires by itself. You are not being asked whether the position is
losing money -- a position can be up and still be finished, and can be
down and still be intact. Judge the trend, not the P&L.

Every number below was computed from exchange data at today's close. You
have no chart and no knowledge of this company, its results or its news.
If something is not in these numbers, you do not know it.

POSITION
  days held: {days_held}
  entry price: {entry}
  stop price: {stop}

READINGS
{features}

WHAT LOSING MOMENTUM LOOKS LIKE
  - Rank sliding through the universe over successive weeks. A name that
    was top-10 and is now mid-pack has lost its relative edge even if the
    price is flat.
  - Price losing its moving averages in order -- through the 20, then the
    50 -- especially with the 50-day slope turning down.
  - Breaking the 20-day Donchian low, or giving back most of the move
    that got it selected.
  - Volatility expanding while the price falls: distribution, not
    accumulation. Check the up/down volume ratio.
  - Fewer up days than down days over the last 21 sessions.

WHAT IS NOT LOSING MOMENTUM
  - An ordinary pullback to a rising 20 or 50-day average.
  - One bad day inside an intact uptrend.
  - Being below the entry price. That is what the stop is for.
  - High volatility on its own. This strategy deliberately holds stocks
    that move.

Be decisive. Selling a stock that was merely resting costs real money in
round-trip costs and forfeits the recovery. Holding a broken one costs
more. `exit_reason` must name the readings that decided it.
`inputs_used` must list the exact field names you relied on.
"""


def exit_judgement(symbol: str, feat: dict, days_held: int,
                   entry: float, stop: float) -> dict:
    """
    Daily off-momentum call on one open position.

    Returns {"exit_now": bool, ...}. On failure or when disabled returns
    exit_now False -- a missing judgement never sells a position.
    """
    def hold(reason):
        return {"symbol": symbol, "exit_now": False, "source": "none",
                "exit_reason": reason, "confidence": None, "model": None}

    if not config.LLM_EXIT_ENABLED:
        return hold("exit judgement disabled")
    if not feat:
        return hold("insufficient price history")

    prompt = _EXIT_PROMPT.format(
        days_held=days_held, entry=f"{entry:,.2f}", stop=f"{stop:,.2f}",
        stop_pct=config.V4_STOP_LOSS_PCT, features=_fmt_features(feat))
    ans = llm.generate_json(prompt, EXIT_SCHEMA)
    if ans is None:
        return hold("every model tier failed")

    used = [u for u in (ans.get("inputs_used") or []) if isinstance(u, str)]
    unknown = [u for u in used if u not in feat]
    if not used or unknown:
        return hold(f"ungrounded inputs_used {unknown or 'empty'}")

    out = {
        "symbol": symbol,
        "exit_now": bool(ans.get("exit_now")),
        "source": "llm",
        "exit_reason": ans.get("exit_reason", ""),
        "confidence": ans.get("confidence"),
        "inputs_used": used,
        "model": ans.get("_model"),
    }
    if out["exit_now"]:
        logger.info("%s: OFF-MOMENTUM exit -- %s", symbol, out["exit_reason"][:120])
    return out


# ---------------------------------------------------------------------------
# Features -- everything the model is allowed to know
# ---------------------------------------------------------------------------

def build_features(symbol: str, as_of: date, price_hist: dict,
                   entry: float = None, signals: pd.DataFrame = None,
                   universe_stats: dict = None) -> dict:
    """
    Compute the numeric payload for one stock.

    price_hist: {date: DataFrame indexed by symbol} with open/high/low/
    close columns, as produced by strategy.load_price_history.

    Returns {} when there is not enough history -- the caller then skips
    the LLM entirely rather than asking it to reason from nothing.
    """
    dates = [d for d in sorted(price_hist) if d <= as_of]
    closes, highs, lows, vols = [], [], [], []
    for d in dates:
        f = price_hist[d]
        if symbol not in f.index:
            continue
        c = f.at[symbol, "close_price"]
        if pd.isna(c) or c <= 0:
            continue
        closes.append(float(c))
        highs.append(float(f.at[symbol, "high_price"])
                     if "high_price" in f.columns and pd.notna(f.at[symbol, "high_price"])
                     else float(c))
        lows.append(float(f.at[symbol, "low_price"])
                    if "low_price" in f.columns and pd.notna(f.at[symbol, "low_price"])
                    else float(c))
        v = f.at[symbol, "volume"] if "volume" in f.columns else np.nan
        vols.append(float(v) if pd.notna(v) else np.nan)

    if len(closes) < 60:
        logger.info("%s: only %d closes, skipping LLM", symbol, len(closes))
        return {}

    s = pd.Series(closes)
    h = pd.Series(highs)
    l = pd.Series(lows)
    last = closes[-1]
    entry = float(entry or last)
    n = len(closes)

    def r2(v):
        return round(float(v), 2) if v is not None and pd.notna(v) else None

    def dma(k):
        return r2(s.tail(k).mean()) if n >= k else None

    def pct_from(v):
        return r2((last / v - 1) * 100) if v else None

    def ret(k):
        return r2((last / closes[-(k + 1)] - 1) * 100) if n > k else None

    def vol(k):
        rr = s.pct_change().dropna().tail(k)
        return r2(rr.std() * np.sqrt(252) * 100) if len(rr) > 5 else None

    def dd(k):
        w = s.tail(k)
        return r2((w / w.cummax() - 1).min() * 100) if len(w) > 5 else None

    def hh(k):
        return r2(h.tail(k).max()) if n >= k else None

    def ll(k):
        return r2(l.tail(k).min()) if n >= k else None

    # True range -> ATR
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
              abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    atr14 = r2(np.mean(tr[-14:])) if len(tr) >= 14 else None
    atr63 = r2(np.mean(tr[-63:])) if len(tr) >= 63 else None

    d20, d50, d200 = dma(20), dma(50), dma(200)
    hi52, lo52 = hh(252), ll(252)
    daily = s.pct_change().dropna()

    feat = {
        # --- price and reference levels -------------------------------
        "last_close": r2(last),
        "reference_entry_price": r2(entry),
        "prev_close": r2(closes[-2]) if n > 1 else None,
        "day_range_pct": r2((highs[-1] - lows[-1]) / closes[-1] * 100),

        # --- trend: moving averages -----------------------------------
        "dma10": dma(10), "dma20": d20, "dma50": d50,
        "dma100": dma(100), "dma200": d200,
        "pct_above_dma10": pct_from(dma(10)),
        "pct_above_dma20": pct_from(d20),
        "pct_above_dma50": pct_from(d50),
        "pct_above_dma100": pct_from(dma(100)),
        "pct_above_dma200": pct_from(d200),
        "ma_stack_bullish": (bool(d20 and d50 and d200 and d20 > d50 > d200)
                             if (d20 and d50 and d200) else None),
        "dma50_slope_21d_pct": (r2((s.tail(50).mean() / s.iloc[-71:-21].mean() - 1) * 100)
                                if n >= 71 else None),

        # --- structure: swing highs / lows -----------------------------
        "donchian_high_20": hh(20), "donchian_high_55": hh(55),
        "donchian_high_100": hh(100),
        "donchian_low_20": ll(20), "donchian_low_55": ll(55),
        "high_52w": hi52, "low_52w": lo52,
        "pct_to_donchian_high_20": r2((hh(20) / last - 1) * 100) if hh(20) else None,
        "pct_to_donchian_high_55": r2((hh(55) / last - 1) * 100) if hh(55) else None,
        "pct_to_high_52w": r2((hi52 / last - 1) * 100) if hi52 else None,
        "pct_above_low_52w": r2((last / lo52 - 1) * 100) if lo52 else None,
        "pct_above_donchian_low_20": r2((last / ll(20) - 1) * 100) if ll(20) else None,

        # --- momentum over several horizons ----------------------------
        "return_5d_pct": ret(5), "return_21d_pct": ret(21),
        "return_63d_pct": ret(63), "return_126d_pct": ret(126),
        "return_252d_pct": ret(252),
        "up_days_pct_last_21": r2((daily.tail(21) > 0).mean() * 100),
        "up_days_pct_last_63": r2((daily.tail(63) > 0).mean() * 100),
        "best_day_last_63_pct": r2(daily.tail(63).max() * 100),
        "worst_day_last_63_pct": r2(daily.tail(63).min() * 100),

        # --- volatility and risk ---------------------------------------
        "atr14": atr14, "atr63": atr63,
        "atr_pct_of_price": r2(atr14 / last * 100) if atr14 else None,
        "realised_vol_21d_annualised_pct": vol(21),
        "realised_vol_63d_annualised_pct": vol(63),
        "realised_vol_252d_annualised_pct": vol(252),
        "vol_21d_vs_63d_ratio": (r2(vol(21) / vol(63))
                                 if vol(21) and vol(63) else None),
        "max_drawdown_63d_pct": dd(63),
        "max_drawdown_252d_pct": dd(252),
        "drawdown_from_52w_high_pct": r2((last / hi52 - 1) * 100) if hi52 else None,
    }
    if entry and entry > 0:
        feat["pct_from_entry"] = r2((last / entry - 1) * 100)

    # --- participation ------------------------------------------------
    vser = pd.Series(vols).dropna()
    if len(vser) >= 60:
        feat["volume_20d_vs_60d_ratio"] = r2(vser.tail(20).mean() / vser.tail(60).mean())
        feat["volume_today_vs_20d_ratio"] = r2(vols[-1] / vser.tail(20).mean()) \
            if pd.notna(vols[-1]) else None
        up = [v for v, c in zip(vols[-21:], daily.tail(21)) if pd.notna(v) and c > 0]
        dn = [v for v, c in zip(vols[-21:], daily.tail(21)) if pd.notna(v) and c <= 0]
        if up and dn:
            feat["up_down_volume_ratio_21d"] = r2(np.mean(up) / np.mean(dn))

    # --- derivatives: ONLY meaningful on expiry day --------------------
    if signals is not None and symbol in signals.index:
        for col in ("rollover", "cost_of_carry"):
            if col in signals.columns and pd.notna(signals.at[symbol, col]):
                feat[col] = r2(signals.at[symbol, col])

    # --- universe context ----------------------------------------------
    if universe_stats:
        feat.update(universe_stats)
    return feat


# ---------------------------------------------------------------------------
# Target -- once, at entry
# ---------------------------------------------------------------------------

_TARGET_PROMPT = """\
You are an equity analyst setting a profit-booking level for one Indian
stock, on the evening of the monthly F&O expiry. The position will be
bought at tomorrow's open and held for roughly 21 trading sessions, until
the next expiry.

ONE QUESTION: how high can this stock plausibly trade at some point in
those 21 sessions?

Not where it will close. Not where it will end up. The highest level it
could realistically TOUCH, because the exit is a resting limit order that
fills the moment price reaches it, even if the stock falls back the same
day.

Every number below was computed from exchange data at today's close. You
have no chart. You have no knowledge of this company, its news, its
results or its sector. If a level is not in these numbers, you do not
know it. Do not recall anything about this stock.

READINGS
{features}

HOW AN ANALYST WOULD READ THIS
Work through it properly rather than applying one rule:

  Reach. ATR is how far this stock actually travels in a day. Over 21
  sessions a trending stock covers several ATRs, a churning one covers
  almost none net. Realised volatility says the same thing annualised.
  Compare the 21-day volatility against the 63-day: expanding volatility
  supports a wider level, contracting argues for a tighter one.

  Resistance. Prior swing highs, the 52-week high, and the upper Donchian
  levels are where sellers previously appeared. A stock 2% below its
  55-day high faces a test there; one already at new highs has open air
  above it and only ATR to constrain it.

  Extension. Distance above the 20, 50, 100 and 200-day averages says how
  stretched it already is. Far above all of them means much of the move
  is behind it. Just reclaiming the 50 with the stack turning up means
  more room. Look at whether the stack is properly ordered and whether
  the 50-day is actually rising.

  Quality of the move. Percentage of up days, up/down volume ratio, and
  best/worst single days separate steady accumulation from one violent
  gap that has already happened. Volume expanding into the move supports
  a higher level; a move on fading volume does not.

  Damage. Recent drawdown and distance below the 52-week high matter.
  A stock 35% off its high in a 63-day drawdown is repairing, not
  advancing, and will struggle to travel far in a month.

  Context. Where this stock's volatility and returns sit against the
  universe medians tells you whether these readings are remarkable or
  ordinary for this basket.

Weigh these against each other. They will conflict — say which you gave
weight to and why.

HARD LIMITS
  - Express the answer as a percentage above the entry price.
  - Minimum {min_pct}%. Below that a target is not worth placing.
  - Maximum {max_pct}%. A hard ceiling. Never exceed it, however strong
    the setup looks.
  - It must be reachable within about 21 sessions. A level the stock
    would need six months to see is a wrong answer, not a cautious one.

`target_basis` must explain your reasoning in two or three sentences,
naming the readings that drove it and the ones you discounted.
`inputs_used` must list the exact field names from READINGS that you
relied on. Never name a field that was not given to you.
"""


def _fmt_features(feat: dict) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in feat.items() if v is not None)


def target_for(symbol: str, entry: float, feat: dict) -> dict:
    """
    Choose a target for a newly-entered position.

    ALWAYS returns a dict with `target_pct` and `source`. On any failure
    -- disabled, no features, model unavailable, validation rejected --
    falls back to config.V4_TARGET_PCT so the position still has a
    target. Never raises.
    """
    flat = float(config.V4_TARGET_PCT)

    def fallback(reason):
        logger.info("%s: using flat %.0f%% target (%s)", symbol, flat, reason)
        return {"symbol": symbol, "target_pct": flat,
                "target_price": round(entry * (1 + flat / 100), 2),
                "source": "flat", "reason": reason,
                "basis": "", "confidence": None, "model": None}

    if not config.LLM_TARGET_ENABLED:
        return fallback("LLM target layer disabled")
    if not feat:
        return fallback("insufficient price history")

    prompt = _TARGET_PROMPT.format(
        entry=f"{entry:,.2f}", features=_fmt_features(feat),
        min_pct=config.LLM_TARGET_MIN_PCT, max_pct=config.LLM_TARGET_MAX_PCT)
    ans = llm.generate_json(prompt, TARGET_SCHEMA)
    if ans is None:
        return fallback("every model tier failed")

    try:
        pct = float(ans["target_pct"])
    except (KeyError, TypeError, ValueError):
        return fallback(f"unusable target_pct {ans.get('target_pct')!r}")

    # Grounding check: a response citing a field we never supplied is
    # the signature of an invented answer, so the whole thing is void.
    used = [u for u in (ans.get("inputs_used") or []) if isinstance(u, str)]
    unknown = [u for u in used if u not in feat]
    if not used or unknown:
        return fallback(f"ungrounded inputs_used {unknown or 'empty'}")

    clamped = False
    if pct > config.LLM_TARGET_MAX_PCT:
        logger.warning("%s: model proposed %.1f%%, clamping to the %.0f%% "
                       "ceiling", symbol, pct, config.LLM_TARGET_MAX_PCT)
        pct, clamped = config.LLM_TARGET_MAX_PCT, True
    if pct < config.LLM_TARGET_MIN_PCT:
        return fallback(f"proposed {pct:.1f}%, below the "
                        f"{config.LLM_TARGET_MIN_PCT:.0f}% floor")

    return {
        "symbol": symbol,
        "target_pct": round(pct, 2),
        "target_price": round(entry * (1 + pct / 100), 2),
        "source": "llm",
        "clamped": clamped,
        "basis": ans.get("target_basis", ""),
        "confidence": ans.get("confidence"),
        "inputs_used": used,
        "model": ans.get("_model"),
        "reason": "",
    }


# ---------------------------------------------------------------------------
# Persistence -- a target is set once and then held fixed
# ---------------------------------------------------------------------------

# (Mid-month replacement selection -- rs_rank(), choose_candidate(), the
# candidate prompt/schema -- removed 01-Sep-2026: confirmed zero callers
# anywhere in the repo, including tests and research scripts. It was
# only ever invoked from daily_report.build(), deleted the same session
# as dead code; mid-cycle redeploy itself has been off in production
# throughout (config.V4_REDEPLOY_ENABLED = False). See CONTEXT.md's
# 01-Sep-2026 session and config.py's matching removal note.)


def load_targets() -> dict:
    path = config.LLM_JUDGMENT_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error("Unreadable %s: %s", path, exc)
        return {}


def save_targets(targets: dict) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.LLM_JUDGMENT_FILE, "w", encoding="utf-8") as fh:
            json.dump(targets, fh, indent=2)
    except OSError as exc:
        logger.error("Could not write %s: %s", config.LLM_JUDGMENT_FILE, exc)


def target_key(symbol: str, entry_date) -> str:
    """A target belongs to one entry. A new entry gets a new target."""
    return f"{symbol}@{entry_date}"


def get_or_set_target(symbol: str, entry: float, entry_date, feat: dict) -> dict:
    """
    Return the target for this position, computing it only the first
    time. Intra-month calls are pure cache reads, so the target cannot
    drift once the position is open.
    """
    targets = load_targets()
    key = target_key(symbol, entry_date)
    if key in targets:
        return targets[key]
    result = target_for(symbol, entry, feat)
    targets[key] = result
    save_targets(targets)
    return result
