"""
Corporate action classification for uncorrected NSE close prices.

THE BUG THIS FIXES
------------------
NSE bhavcopy is not corporate-action adjusted. `strategy.split_adjust`
currently assumes ANY day-on-day ratio outside 0.6-1.8 is a split and
back-adjusts the prior history by that ratio. It cannot tell a 5:1 split
from a genuine collapse, so a real -80% crash is silently laundered into
a clean series -- and the stock then scores as high-volatility and enters
the basket. Over the 388 cached trading days there are 20 such breaches.

HOW IT WORKS
------------
Gemini is given the price move and EVERY corporate action NSE filed in a
window around that date, and asked which combination explains the move.
There is deliberately no deterministic parser: the hard cases are
selection and composition, not extraction. Measured on the 20 real
breaches (01-Aug-2026):

  - 15 are a single unambiguous split or bonus
  -  3 have multiple filings in the window and need the right one chosen,
     or several composed. BAJFINANCE 16-Jun-2025 is a 2->1 face value
     split AND a Bonus 4:1 on the SAME DAY: 0.5 * 0.2 = 0.1, matching the
     observed 0.0996. Picking either filing alone is wrong by 2x or 5x.
  -  2 are demergers whose subject line is the single word "Demerger",
     with no ratio present in the data at all. The honest answer there is
     UNKNOWN -- do not adjust.

THE RECONCILIATION FLAG
-----------------------
The model is told to reconcile its ratio against the observed move, and
this module INDEPENDENTLY recomputes that residual afterwards. The check
is reported, not enforced: a non-reconciling answer is still returned,
flagged, and surfaced in the evening alert. That is a deliberate choice
-- the alternative is silently discarding the model's answer, which
recreates the very "silent wrong adjustment" failure this module exists
to remove.
"""

import json
import logging
from datetime import date

import config
import llm
import nse_corporate

logger = logging.getLogger("momentum_tracker.corporate_actions")

CLASSIFICATIONS = (
    "SPLIT", "BONUS", "COMPOSITE", "RIGHTS",
    "DEMERGER", "DIVIDEND", "GENUINE_MOVE", "UNKNOWN",
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "classification": {"type": "STRING", "enum": list(CLASSIFICATIONS)},
        "adjustment_ratio": {"type": "NUMBER"},
        "reconciles": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
        "evidence": {"type": "STRING"},
    },
    "required": ["classification", "adjustment_ratio", "reconciles",
                 "confidence", "reasoning", "evidence"],
}

_PROMPT = """\
You are adjusting an Indian equity price series for corporate actions.

NSE bhavcopy close prices are NOT corporate-action adjusted. When a stock
splits or issues a bonus, the close drops mechanically overnight. That
drop is not a real loss and must be removed before computing returns or
volatility, or the stock is scored as both the worst performer and the
most volatile in the universe. Both would be wrong.

THE PRICE MOVE TO EXPLAIN
  Symbol:            {symbol}
  Date:              {as_of}
  Previous close:    {prev_close}
  This close:        {close}
  Observed ratio:    {observed:.6f}   (close / previous close)

CORPORATE ACTIONS NSE FILED IN THE SURROUNDING WINDOW
{filings}

YOUR TASK
Decide which of these filings, if any, explains the move, and return the
`adjustment_ratio`: the factor that PRIOR closes must be MULTIPLIED by so
the series becomes continuous across this date.

HOW TO COMPUTE IT
  - Face value split from Rs A to Rs B  ->  ratio = B / A
      (Rs 10 to Rs 2 gives 0.2; Rs 5 to Re 1 gives 0.2)
  - Bonus of a:b (a free shares for every b held)  ->  ratio = b / (a + b)
      (Bonus 1:1 gives 0.5; Bonus 2:1 gives 0.3333; Bonus 4:1 gives 0.2)
  - MULTIPLE ACTIONS ON THE SAME EX-DATE COMPOSE MULTIPLICATIVELY.
      A 2->1 split together with a Bonus 4:1 gives 0.5 * 0.2 = 0.1.
      This is common and is the single most important case to get right.
  - Ordinary dividends do NOT justify an adjustment. A dividend is a real
    transfer of value. Ignore dividend filings unless the payment is so
    large relative to the price that it alone explains the move.
  - No filing explains the move  ->  classification GENUINE_MOVE,
    adjustment_ratio 1.0. A real crash MUST be left unadjusted. This is
    the most important case to get right after composition, because
    wrongly "correcting" a genuine collapse hides a catastrophic loss.
  - A demerger whose filing contains no ratio  ->  classification UNKNOWN,
    adjustment_ratio 1.0. Do not invent a number. Say you cannot tell.

THE RECONCILIATION REQUIREMENT
Your adjustment_ratio must be consistent with the observed ratio of
{observed:.6f}. They will not match exactly, because the stock also moves
on its own merits that day -- a 2:1 bonus implying 0.3333 against an
observed 0.3499 is a good match with a genuine +5% move on top.

Set `reconciles` true only if your ratio is within roughly
{tolerance:.0%} of the observed ratio. If your best reading of the
filings produces a ratio that does NOT reconcile, still return it, set
`reconciles` false, and say so plainly in `reasoning`. Do not bend the
arithmetic to force agreement, and do not fabricate a ratio to fill the
gap -- an honest UNKNOWN is more useful than a plausible wrong number.

`evidence` must quote the exact filing text you relied on, or state that
no filing explained the move.
"""


def _format_filings(filings: list) -> str:
    if not filings:
        return "  (none -- NSE filed no corporate action in this window)"
    lines = []
    for f in filings:
        fv = f.get("faceVal")
        extra = f" [faceVal={fv}]" if fv not in (None, "", "-") else ""
        lines.append(f"  - exDate={f.get('exDate')} recDate={f.get('recDate')}"
                     f" series={f.get('series')}{extra}\n"
                     f"    subject: {f.get('subject')}")
    return "\n".join(lines)


def build_prompt(symbol: str, as_of: date, prev_close: float,
                 close: float, filings: list) -> str:
    return _PROMPT.format(
        symbol=symbol,
        as_of=as_of,
        prev_close=f"{prev_close:,.2f}",
        close=f"{close:,.2f}",
        observed=close / prev_close,
        filings=_format_filings(filings),
        tolerance=config.CORP_ACTION_RECONCILE_TOLERANCE,
    )


def _unknown(symbol, as_of, observed, reason):
    return {
        "symbol": symbol,
        "date": str(as_of),
        "observed_ratio": observed,
        "classification": "UNKNOWN",
        "adjustment_ratio": 1.0,
        "reconciles": False,
        "residual": None,
        "confidence": 0.0,
        "reasoning": reason,
        "evidence": "",
        "model": None,
        "flagged": True,
    }


def classify(symbol: str, as_of: date, prev_close: float, close: float,
             session=None) -> dict:
    """
    Explain one day-on-day price ratio breach.

    Always returns a dict. On any failure -- feed down, LLM exhausted,
    classifier disabled -- returns UNKNOWN with adjustment_ratio 1.0 and
    flagged=True, which means "do not adjust, tell the user". Never
    raises: a classification failure must not abort a strategy run.
    """
    if prev_close is None or close is None or prev_close <= 0 or close <= 0:
        return _unknown(symbol, as_of, None, "Non-positive or missing close price")

    observed = close / prev_close

    if not config.CORP_ACTION_LLM_ENABLED:
        return _unknown(symbol, as_of, observed,
                        "Classifier disabled (config.CORP_ACTION_LLM_ENABLED)")

    try:
        filings = nse_corporate.fetch_corporate_actions(symbol, as_of, session)
    except nse_corporate.CorpFetchError as exc:
        logger.error("Corporate action feed failed for %s %s: %s",
                     symbol, as_of, exc)
        return _unknown(symbol, as_of, observed,
                        f"Corporate action feed unavailable: {exc}")

    prompt = build_prompt(symbol, as_of, prev_close, close, filings)
    answer = llm.generate_json(prompt, RESPONSE_SCHEMA)
    if answer is None:
        return _unknown(symbol, as_of, observed,
                        "Every Gemini tier failed for this classification")

    try:
        ratio = float(answer["adjustment_ratio"])
    except (KeyError, TypeError, ValueError):
        return _unknown(symbol, as_of, observed,
                        f"Model returned an unusable adjustment_ratio: "
                        f"{answer.get('adjustment_ratio')!r}")

    if ratio <= 0:
        return _unknown(symbol, as_of, observed,
                        f"Model returned a non-positive ratio {ratio}")

    # Independent arithmetic check. Reported, never enforced -- see the
    # module docstring for why.
    residual = abs(ratio / observed - 1.0)
    reconciles = residual <= config.CORP_ACTION_RECONCILE_TOLERANCE
    classification = answer.get("classification", "UNKNOWN")

    # GENUINE_MOVE and UNKNOWN both mean "ratio 1.0, do not adjust", so a
    # residual against the observed move is meaningless for them.
    no_adjustment = classification in ("GENUINE_MOVE", "UNKNOWN")
    flagged = (not reconciles) and not no_adjustment

    if flagged:
        logger.warning(
            "%s %s: model ratio %.4f does not reconcile with observed %.4f "
            "(residual %.1f%%, class=%s) -- applying anyway, flagged",
            symbol, as_of, ratio, observed, residual * 100, classification)

    if answer.get("reconciles") is not reconciles and not no_adjustment:
        logger.info("%s %s: model self-reported reconciles=%s, arithmetic "
                    "says %s", symbol, as_of, answer.get("reconciles"),
                    reconciles)

    return {
        "symbol": symbol,
        "date": str(as_of),
        "observed_ratio": observed,
        "classification": classification,
        "adjustment_ratio": 1.0 if no_adjustment else ratio,
        "reconciles": reconciles,
        "residual": residual,
        "confidence": answer.get("confidence"),
        "reasoning": answer.get("reasoning", ""),
        "evidence": answer.get("evidence", ""),
        "filings": len(filings),
        "model": answer.get("_model"),
        "flagged": flagged,
    }
