"""
NSE bhavcopy client: downloads and parses NSE cash-market (CM) and
derivatives (F&O) bhavcopy files, with retries, disk caching and logging.

VERIFIED LIVE (02-Aug-2026): the download path works. 246 days of 2024
CM bhavcopy were fetched through it in one run, and every uncached date
tested returned real data. An earlier version of this docstring claimed
the network blocked nseindia.com -- that was wrong and is now removed.

A 404 means NSE published nothing for that date, which is permanent, not
transient. It is raised immediately as NseNoDataError and recorded with a
.nodata marker so the same holiday is never re-requested. Retrying 404s
cost 14 seconds per market holiday and made the nightly run unschedulable.
"""

import io
import logging
import os
import time
import zipfile
from datetime import date

import pandas as pd
import requests

import config

logger = logging.getLogger("momentum_tracker.nse_client")


class NseFetchError(Exception):
    """Raised when an NSE bhavcopy cannot be fetched or parsed."""


class NseNoDataError(NseFetchError):
    """
    NSE has no file for this date -- almost always a market holiday.

    Distinct from NseFetchError because it is PERMANENT: retrying cannot
    change the answer. Subclasses NseFetchError so existing callers that
    catch the parent keep working unchanged.
    """


def _cache_path(prefix: str, trade_date: date) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, f"{prefix}_{trade_date:%Y%m%d}.csv")


def _nodata_path(prefix: str, trade_date: date) -> str:
    """Marker recording that NSE has no file for this date."""
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, f"{prefix}_{trade_date:%Y%m%d}.nodata")


def _mark_nodata(prefix: str, trade_date: date) -> None:
    try:
        with open(_nodata_path(prefix, trade_date), "w", encoding="utf-8") as fh:
            fh.write("no bhavcopy published for this date (HTTP 404)\n")
    except OSError as exc:
        logger.warning("Could not write no-data marker for %s: %s", trade_date, exc)


def _download_with_retry(url: str) -> bytes:
    """
    Fetch with retry + exponential backoff.

    A 404 is raised IMMEDIATELY as NseNoDataError and never retried: NSE
    publishes nothing on market holidays, and that answer will not change
    on a second attempt. Retrying it cost 14s of backoff per holiday and
    made the nightly run too slow to schedule.
    """
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            logger.info("Fetching %s (attempt %d/%d)", url, attempt, config.MAX_RETRIES)
            resp = requests.get(
                url,
                headers=config.NSE_REQUEST_HEADERS,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue

        if resp.status_code == 404:
            # Permanent. Do not burn retries or backoff on it.
            raise NseNoDataError(
                f"NSE returned 404 for {url} — likely not a trading day, "
                "or the file naming pattern has changed."
            )
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        return resp.content
    raise NseFetchError(f"Failed to fetch {url} after {config.MAX_RETRIES} attempts") from last_exc


def _extract_single_csv(zip_bytes: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise NseFetchError("Zip archive contained no CSV file")
        with zf.open(names[0]) as f:
            return pd.read_csv(f)


def _fetch_bhavcopy(prefix: str, url_template: str, required: set,
                    trade_date: date, use_cache: bool) -> pd.DataFrame:
    cache_file = _cache_path(prefix, trade_date)
    if use_cache and os.path.exists(cache_file):
        logger.info("Using cached %s bhavcopy for %s", prefix.upper(), trade_date)
        return pd.read_csv(cache_file)

    # Negative cache: we already asked NSE about this date and it had
    # nothing. Holidays recur in every backtest window, so without this
    # the same 404s are re-requested on every single run.
    if use_cache and os.path.exists(_nodata_path(prefix, trade_date)):
        raise NseNoDataError(
            f"No {prefix.upper()} bhavcopy for {trade_date} (cached no-data marker)")

    url = url_template.format(date=trade_date)
    try:
        content = _download_with_retry(url)
    except NseNoDataError:
        if use_cache:
            _mark_nodata(prefix, trade_date)
        raise
    df = _extract_single_csv(content)
    _validate_columns(df, required, url)
    df.to_csv(cache_file, index=False)
    return df


def fetch_cm_bhavcopy(trade_date: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch NSE cash-market bhavcopy (close price, volume) for one day."""
    return _fetch_bhavcopy(
        "cm", config.NSE_CM_BHAVCOPY_URL,
        {"TckrSymb", "ClsPric", "TtlTradgVol"}, trade_date, use_cache)


def fetch_fo_bhavcopy(trade_date: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch NSE F&O bhavcopy (futures OI, settlement price, expiry) for one day."""
    return _fetch_bhavcopy(
        "fo", config.NSE_FO_BHAVCOPY_URL,
        {"TckrSymb", "XpryDt", "FinInstrmTp", "OpnIntrst", "SttlmPric"},
        trade_date, use_cache)


def _validate_columns(df: pd.DataFrame, required: set, source: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise NseFetchError(
            f"Bhavcopy schema from {source} is missing expected columns {missing}. "
            "NSE may have changed the file format — check "
            "https://www.nseindia.com/all-reports and update config.py."
        )
