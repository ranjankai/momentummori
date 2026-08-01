"""
NSE corporate-data client: corporate actions and ASM surveillance.

Separate from nse_client.py on purpose -- that module owns bhavcopy ZIPs
on a fixed URL template, this one owns NSE's JSON web APIs, which behave
differently and fail differently.

VERIFIED 01-Aug-2026
--------------------
- /api/corporates-corporateActions -> 200, returns one record per action
  with exDate, recDate, subject, faceVal, series, isin.
- /api/reportASM -> 200, {"longterm": {...}, "shortterm": {...}}, 149 and
  37 entries respectively.
- The NSE HOMEPAGE returns 403 while both API paths return 200. Do NOT
  gate these calls on a homepage "cookie warm-up" -- it fails and buys
  nothing.

CAVEAT
------
reportASM is a CURRENT SNAPSHOT. NSE publishes no ASM history, so the
surveillance veto cannot be backtested. See CONTEXT.md.
"""

import json
import logging
import os
import time
from datetime import date, timedelta

import requests

import config

logger = logging.getLogger("momentum_tracker.nse_corporate")


class CorpFetchError(Exception):
    """Raised when an NSE corporate endpoint cannot be fetched or parsed."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(config.NSE_REQUEST_HEADERS)
    return s


def _get_json(url: str, session: requests.Session = None):
    """GET with retry + backoff. Raises CorpFetchError when exhausted."""
    session = session or _session()
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                text = resp.text.strip()
                if not text.startswith(("[", "{")):
                    raise CorpFetchError(
                        f"{url} returned 200 but not JSON (got {text[:80]!r}) "
                        "-- NSE may be serving an interstitial."
                    )
                return resp.json()
            last = CorpFetchError(f"HTTP {resp.status_code} from {url}")
            logger.warning("Attempt %d/%d: HTTP %d for %s",
                           attempt, config.MAX_RETRIES, resp.status_code, url)
        except (requests.RequestException, ValueError, CorpFetchError) as exc:
            last = exc
            logger.warning("Attempt %d/%d failed for %s: %s",
                           attempt, config.MAX_RETRIES, url, exc)
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise CorpFetchError(f"Failed to fetch {url} after "
                         f"{config.MAX_RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------

def _corp_cache_path(symbol: str, as_of: date) -> str:
    os.makedirs(config.CORP_CACHE_DIR, exist_ok=True)
    return os.path.join(config.CORP_CACHE_DIR, f"ca_{symbol}_{as_of:%Y%m%d}.json")


def fetch_corporate_actions(symbol: str, as_of: date,
                            session: requests.Session = None) -> list:
    """
    Every corporate action filed in a window around `as_of`.

    Returns a list of dicts (possibly empty). Empty is meaningful: it says
    NSE knows of no action explaining a price move on that date, which is
    the evidence that the move was genuine.

    Cached to disk per (symbol, date) -- historical filings never change,
    so a backtest re-run costs no network.
    """
    path = _corp_cache_path(symbol, as_of)
    if config.CORP_CACHE_ENABLED and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable corp cache %s: %s", path, exc)

    frm = (as_of - timedelta(days=config.CORP_ACTION_LOOKBACK_DAYS))
    to = (as_of + timedelta(days=config.CORP_ACTION_LOOKAHEAD_DAYS))
    url = config.NSE_CORP_ACTIONS_URL.format(
        symbol=symbol,
        from_date=frm.strftime("%d-%m-%Y"),
        to_date=to.strftime("%d-%m-%Y"),
    )
    data = _get_json(url, session)
    if not isinstance(data, list):
        raise CorpFetchError(
            f"Unexpected corporate-actions payload for {symbol} {as_of}: "
            f"{type(data).__name__}"
        )

    records = [
        {
            "exDate": r.get("exDate"),
            "recDate": r.get("recDate"),
            "subject": (r.get("subject") or "").strip(),
            "faceVal": r.get("faceVal"),
            "series": r.get("series"),
            "comp": r.get("comp"),
        }
        for r in data
    ]
    if config.CORP_CACHE_ENABLED:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2)
        except OSError as exc:
            logger.warning("Could not write corp cache %s: %s", path, exc)

    logger.info("%s %s: %d corporate action(s) in window",
                symbol, as_of, len(records))
    return records


# ---------------------------------------------------------------------------
# ASM surveillance
# ---------------------------------------------------------------------------

def fetch_asm_symbols(session: requests.Session = None) -> dict:
    """
    Current ASM list as {SYMBOL: {"stage":..., "list":..., "desc":...}}.

    NSE's payload carries companyName and isin but the symbol field is
    inconsistently populated, so we read `symbol` where present and fall
    back to `companyName` only for logging -- a name we cannot key by
    symbol is skipped rather than guessed at, because a wrong veto is
    worse than a missed one.
    """
    data = _get_json(config.NSE_ASM_URL, session)
    if not isinstance(data, dict):
        raise CorpFetchError(f"Unexpected ASM payload: {type(data).__name__}")

    out, skipped = {}, 0
    buckets = [("longterm", True)]
    if config.VETO_INCLUDE_SHORTTERM_ASM:
        buckets.append(("shortterm", True))

    for bucket, _ in buckets:
        for row in (data.get(bucket) or {}).get("data", []) or []:
            sym = (row.get("symbol") or "").strip().upper()
            if not sym:
                skipped += 1
                continue
            out[sym] = {
                "stage": (row.get("asmSurvIndicator") or "").strip(),
                "list": bucket,
                "desc": (row.get("survDesc") or "").strip(),
                "as_of": (row.get("asmTime") or "").strip(),
            }
    logger.info("ASM list: %d symbols usable, %d rows lacked a symbol field",
                len(out), skipped)
    return out
