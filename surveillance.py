"""
Pre-order surveillance veto.

WHAT IT DOES
------------
Given a ranked basket, drops names currently under NSE's Additional
Surveillance Measure. EXCLUSION ONLY -- it can never promote a name or
reorder the ranking. A vetoed slot is refilled from the next eligible
name in the full ranking, or left short if none qualifies.

WHY EXCLUSION ONLY
------------------
Discretionary override of a mechanical ranking reliably degrades it. The
documented exception is the "broken leg" case: a fact the model
structurally cannot see. That asymmetry only justifies dropping a name,
never promoting one, because promotion requires positive information the
ranking lacks -- a far stronger claim. See CONTEXT.md.

WHY ASM SPECIFICALLY
--------------------
Stage II and above carry 100% margin and periodic call-auction or
trade-for-trade settlement. That breaks the strategy's core assumption
that a 5% resting stop fills near its trigger: in a call auction there is
no continuous book to fill against.

HARD LIMITATION
---------------
NSE publishes ASM as a CURRENT SNAPSHOT with no history. This veto
therefore CANNOT be backtested -- the +32.34% table in CONTEXT.md does
not include it and cannot be re-run with it. You are adopting it on
reasoning, not evidence. Set config.VETO_ENABLED = False to disable.
"""

import logging

import config
import nse_corporate

logger = logging.getLogger("momentum_tracker.surveillance")


def fetch_vetoed_symbols(session=None) -> dict:
    """
    Symbols currently disqualified, as {SYMBOL: reason}.

    Returns {} when the veto is disabled OR the feed is unreachable. That
    is deliberate: a surveillance outage must not silently empty your
    basket. Failing open is logged loudly so the evening note can say the
    check did not run.
    """
    if not config.VETO_ENABLED:
        logger.info("Surveillance veto disabled in config")
        return {}
    try:
        asm = nse_corporate.fetch_asm_symbols(session)
    except nse_corporate.CorpFetchError as exc:
        logger.error("ASM feed unavailable, FAILING OPEN (no veto applied): %s", exc)
        return {}

    vetoed = {}
    for sym, meta in asm.items():
        stage = meta.get("stage", "")
        if stage in config.VETO_ASM_STAGES:
            vetoed[sym] = f"ASM {stage} ({meta['list']}, as of {meta.get('as_of','?')})"
    logger.info("Surveillance veto: %d of %d ASM symbols disqualified",
                len(vetoed), len(asm))
    return vetoed


def apply_veto(basket, full_ranking_symbols, sector_map=None, session=None):
    """
    Drop vetoed names from `basket` and backfill from `full_ranking_symbols`.

    basket: DataFrame from strategy.rank_universe (has a `symbol` column).
    full_ranking_symbols: ordered symbols from the complete ranking.

    Returns (kept_basket_symbols, dropped, added, veto_ran). `veto_ran` is
    False when the feed failed, so the caller can say so rather than
    implying a clean check.
    """
    vetoed = fetch_vetoed_symbols(session)
    veto_ran = bool(vetoed) or not config.VETO_ENABLED
    original = list(basket["symbol"]) if hasattr(basket, "columns") else list(basket)

    if not vetoed:
        return original, [], [], veto_ran

    kept = [s for s in original if s not in vetoed]
    dropped = [(s, vetoed[s]) for s in original if s in vetoed]

    max_per_sector = max(1, int(config.PORTFOLIO_SIZE
                                * config.MAX_SECTOR_WEIGHT_PCT / 100))
    counts = {}
    if sector_map:
        for s in kept:
            sec = sector_map.get(s, f"Unclassified:{s}")
            counts[sec] = counts.get(sec, 0) + 1

    added = []
    for cand in full_ranking_symbols:
        if len(kept) >= config.PORTFOLIO_SIZE:
            break
        if cand in kept or cand in vetoed:
            continue
        if sector_map:
            sec = sector_map.get(cand, f"Unclassified:{cand}")
            if counts.get(sec, 0) >= max_per_sector:
                continue
            counts[sec] = counts.get(sec, 0) + 1
        kept.append(cand)
        added.append(cand)

    if len(kept) < config.PORTFOLIO_SIZE:
        logger.warning("Veto left the basket short: %d of %d slots filled",
                       len(kept), config.PORTFOLIO_SIZE)
    for sym, why in dropped:
        logger.info("VETOED %s -- %s", sym, why)
    return kept, dropped, added, veto_ran
