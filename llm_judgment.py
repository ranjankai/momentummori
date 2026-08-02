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
  EXIT    every day, mid-month only. Purely additive to the 5% stop: it
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


# ---------------------------------------------------------------------------
# Features -- everything the model is allowed to know
# ---------------------------------------------------------------------------

def build_features(symbol: str, as_of: date, price_hist: dict,
                   entry: float = None, signals: pd.DataFrame = None) -> dict:
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
    last = closes[-1]
    entry = float(entry or last)

    def dma(n):
        return round(float(s.tail(n).mean()), 2) if len(s) >= n else None

    def pct_from(v):
        return round((last / v - 1) * 100, 2) if v else None

    # True range -> ATR14
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    atr = round(float(np.mean(tr[-14:])), 2) if len(tr) >= 14 else None

    rets = s.pct_change().dropna()
    feat = {
        "last_close": round(last, 2),
        "entry_price": round(entry, 2),
        "pct_from_entry": round((last / entry - 1) * 100, 2),
        "dma20": dma(20), "dma50": dma(50), "dma200": dma(200),
        "pct_above_dma20": pct_from(dma(20)),
        "pct_above_dma50": pct_from(dma(50)),
        "pct_above_dma200": pct_from(dma(200)),
        "donchian_high_20": round(max(highs[-20:]), 2),
        "donchian_high_55": round(max(highs[-55:]), 2) if len(highs) >= 55 else None,
        "donchian_low_20": round(min(lows[-20:]), 2),
        "pct_to_donchian_high_20": round((max(highs[-20:]) / last - 1) * 100, 2),
        "pct_to_donchian_high_55": (round((max(highs[-55:]) / last - 1) * 100, 2)
                                    if len(highs) >= 55 else None),
        "atr14": atr,
        "atr_pct_of_price": round(atr / last * 100, 2) if atr else None,
        "realised_vol_63d_annualised_pct": round(
            float(rets.tail(63).std() * np.sqrt(252) * 100), 2),
        "return_21d_pct": round((last / closes[-22] - 1) * 100, 2) if len(closes) > 22 else None,
        "return_63d_pct": round((last / closes[-64] - 1) * 100, 2) if len(closes) > 64 else None,
        "max_drawdown_63d_pct": round(
            float((s.tail(63) / s.tail(63).cummax() - 1).min() * 100), 2),
    }
    if len(vols) >= 20 and not all(pd.isna(vols[-20:])):
        v20 = pd.Series(vols[-20:]).dropna()
        v60 = pd.Series(vols[-60:]).dropna()
        if len(v20) and len(v60):
            feat["volume_20d_vs_60d_ratio"] = round(float(v20.mean() / v60.mean()), 2)

    if signals is not None and symbol in signals.index:
        for col in ("rollover", "cost_of_carry"):
            if col in signals.columns and pd.notna(signals.at[symbol, col]):
                feat[col] = round(float(signals.at[symbol, col]), 2)
    return feat


# ---------------------------------------------------------------------------
# Target -- once, at entry
# ---------------------------------------------------------------------------

_TARGET_PROMPT = """\
You are setting a profit target for one Indian equity position that was
just entered. You will be given ONLY the numbers below, all computed from
exchange data today. You have no chart and no other knowledge of this
stock. Do not use any recollection of the company; if a level is not in
the numbers, you do not know it.

POSITION
  entry price: {entry}

COMPUTED READINGS
{features}

TASK
Choose ONE profit target, expressed as a percentage above the entry
price, by weighing these readings against each other. Think like a
technical analyst blending several signals into a single level:

  - Prior swing highs (Donchian) are natural resistance and a common
    place to book profit.
  - ATR tells you how far this stock actually travels. A target many
    ATRs away is unlikely to be reached inside a month.
  - Distance above the moving averages tells you how extended it already
    is. Something far above its 200-day has less room than something
    just breaking out.
  - High realised volatility supports a wider target; low volatility
    argues for a tighter one.
  - A deep recent drawdown argues for caution.

HARD LIMITS
  - Minimum {min_pct}%. Below this a target is not worth placing.
  - Maximum {max_pct}%. This is a hard ceiling, not a guideline. Never
    propose more, however strong the setup looks.
  - The holding period is roughly one month. Set something reachable in
    that window, not an eventual price.

`target_basis` must name the readings you weighed and say why, in one or
two sentences. `inputs_used` must list the exact field names from the
COMPUTED READINGS block that you relied on -- do not list fields you did
not use, and do not name a field that was not given to you.
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

# ---------------------------------------------------------------------------
# Mid-month replacement: price-based relative strength, cash data only
# ---------------------------------------------------------------------------

def rs_rank(as_of: date, symbols, price_hist: dict) -> pd.DataFrame:
    """
    Mechanical relative-strength ranking on CASH data only, recomputed for
    the day it is called.

    Deliberately no rollover and no cost of carry. Measured over the
    Jul-2026 cycle, rollover's rank ordering is frozen for three weeks
    between expiries (rank correlation 0.84-0.95 snapshot to snapshot) and
    its mid-cycle value has -0.1 rank correlation with the expiry value.
    Feeding it into a mid-month decision adds staleness, not information.

    Returns a DataFrame indexed by symbol, sorted best first, with the
    component readings retained so they can be shown to the LLM.
    """
    rows = []
    dates = [d for d in sorted(price_hist) if d <= as_of]
    for sym in symbols:
        closes = []
        for d in dates:
            f = price_hist[d]
            if sym not in f.index:
                continue
            c = f.at[sym, "close_price"]
            if pd.notna(c) and c > 0:
                closes.append(float(c))
        if len(closes) < 130:
            continue
        s = pd.Series(closes)
        last = closes[-1]
        rets = s.pct_change().dropna()
        rows.append({
            "symbol": sym,
            "last": round(last, 2),
            "ret_126d": round((last / closes[-127] - 1) * 100, 2),
            "ret_63d": round((last / closes[-64] - 1) * 100, 2),
            "ret_21d": round((last / closes[-22] - 1) * 100, 2),
            "above_dma50": round((last / s.tail(50).mean() - 1) * 100, 2),
            "above_dma200": round((last / s.tail(200).mean() - 1) * 100, 2)
                            if len(s) >= 200 else None,
            "volatility": round(float(rets.tail(63).std() * np.sqrt(252) * 100), 2),
            "drawdown_63d": round(
                float((s.tail(63) / s.tail(63).cummax() - 1).min() * 100), 2),
        })
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("symbol")

    def z(col):
        sd = df[col].std()
        return (df[col] - df[col].mean()) / sd if sd else df[col] * 0

    w = config.RS_WEIGHTS
    df["rs_score"] = sum(w[c] * z(c) for c in w)
    return df.sort_values("rs_score", ascending=False)


_CANDIDATE_PROMPT = """\
A position exited mid-month and one slot of a ten-stock Indian equity
portfolio is now empty. It must be refilled at tomorrow's open. Choose
which stock to buy.

You are given a shortlist that has ALREADY been screened: every name
below is eligible, liquid, passes the sector cap, and ranks near the top
on price-based relative strength. Your job is to pick the best of a good
set, not to find a bargain.

All figures are computed from today's exchange close. You have no chart
and no other knowledge of these companies. If something is not in the
numbers, you do not know it.

SHORTLIST
{candidates}

HOW TO WEIGH IT
  - Sustained strength over 6 and 3 months matters more than a 21-day
    pop, which is often already exhausted.
  - Price above the 50- and 200-day averages confirms the trend is
    intact rather than a bounce inside a decline.
  - This strategy deliberately favours stocks that move: higher realised
    volatility is a positive, not a risk to be avoided.
  - A deep recent drawdown argues against, even where returns look good
    -- it usually means the move has already broken once.
  - The position carries a 5% stop from entry. A name that routinely
    swings more than that in a day will likely be stopped out on noise.

Return the SYMBOL exactly as written in the shortlist. `rationale` should
say in one or two sentences why this one over the others. `inputs_used`
must list the exact field names you relied on.
"""

CANDIDATE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "symbol": {"type": "STRING"},
        "rationale": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
        "inputs_used": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["symbol", "rationale", "confidence", "inputs_used"],
}

_CAND_FIELDS = ("last", "ret_126d", "ret_63d", "ret_21d", "above_dma50",
                "above_dma200", "volatility", "drawdown_63d")


def choose_candidate(eligible: list, rs: pd.DataFrame) -> dict:
    """
    Pick the replacement for a freed slot.

    `eligible` is the already-screened list of symbols, in RS order.
    The mechanical answer is simply `eligible[0]`; the LLM may pick a
    different name from the shortlist, and anything else is rejected.

    A SLOT IS NEVER LEFT EMPTY. If the LLM is disabled, fails, or returns
    something not on the shortlist, the top-ranked eligible name is used.
    Only a completely empty `eligible` list leaves the slot unfilled, and
    the caller relaxes the sector cap before allowing that.
    """
    if not eligible:
        return {"symbol": None, "source": "none",
                "rationale": "no eligible candidate in the universe"}

    top = eligible[0]

    def mechanical(reason):
        return {"symbol": top, "source": "rs", "rationale": reason,
                "confidence": None, "model": None}

    if not config.LLM_CANDIDATE_ENABLED:
        return mechanical("LLM candidate selection disabled; top RS name")

    shortlist = eligible[:config.CANDIDATE_SHORTLIST_N]
    lines = []
    for sym in shortlist:
        if sym not in rs.index:
            continue
        r = rs.loc[sym]
        bits = ", ".join(f"{f}={r[f]}" for f in _CAND_FIELDS
                         if f in rs.columns and pd.notna(r[f]))
        lines.append(f"  {sym}: {bits}")
    if not lines:
        return mechanical("no readings available for the shortlist")

    ans = llm.generate_json(
        _CANDIDATE_PROMPT.format(candidates="\n".join(lines)),
        CANDIDATE_SCHEMA)
    if ans is None:
        return mechanical("every model tier failed; top RS name")

    pick = str(ans.get("symbol", "")).strip().upper()
    if pick not in {s.upper() for s in shortlist}:
        logger.warning("Model returned %r which is not on the shortlist; "
                       "falling back to top RS name %s", pick, top)
        return mechanical(f"model returned off-shortlist {pick!r}; top RS name")

    used = [u for u in (ans.get("inputs_used") or []) if isinstance(u, str)]
    unknown = [u for u in used if u not in _CAND_FIELDS]
    if not used or unknown:
        return mechanical(f"ungrounded inputs_used {unknown or 'empty'}; top RS name")

    actual = next(s for s in shortlist if s.upper() == pick)
    logger.info("LLM picked %s over top-RS %s", actual, top)
    return {"symbol": actual, "source": "llm",
            "rationale": ans.get("rationale", ""),
            "confidence": ans.get("confidence"),
            "inputs_used": used,
            "rs_rank_of_pick": shortlist.index(actual) + 1,
            "model": ans.get("_model")}


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
