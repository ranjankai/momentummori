"""
NSE bhavcopy client: downloads and parses NSE cash-market (CM) and
derivatives (F&O) bhavcopy files, with retries, disk caching and logging.

NOTE (unverified): this sandbox's network is allowlisted and blocks
nseindia.com, so the live download path below has NOT been exercised
against a real NSE response in this session. The parsing logic has been
verified against synthetic CSVs matching NSE's documented UDiFF bhavcopy
schema (see tests/test_scoring.py). Run scripts/smoke_test_live.py on a
machine with normal internet access to confirm the URL template and
column names are still current before relying on this for real trading
decisions.
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


def _cache_path(prefix: str, trade_date: date) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, f"{prefix}_{trade_date:%Y%m%d}.csv")


def _download_with_retry(url: str) -> bytes:
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            logger.info("Fetching %s (attempt %d/%d)", url, attempt, config.MAX_RETRIES)
            resp = requests.get(
                url,
                headers=config.NSE_REQUEST_HEADERS,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code == 404:
                raise NseFetchError(
                    f"NSE returned 404 for {url} — likely not a trading day, "
                    "or the file naming pattern has changed."
                )
            resp.raise_for_status()
            return resp.content
        except (requests.RequestException, NseFetchError) as exc:
            last_exc = exc
            logger.warning("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < config.MAX_RETRIES:
                sleep_s = config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(sleep_s)
    raise NseFetchError(f"Failed to fetch {url} after {config.MAX_RETRIES} attempts") from last_exc


def _extract_single_csv(zip_bytes: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise NseFetchError("Zip archive contained no CSV file")
        with zf.open(names[0]) as f:
            return pd.read_csv(f)


def fetch_cm_bhavcopy(trade_date: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch NSE cash-market bhavcopy (close price, volume) for one day."""
    cache_file = _cache_path("cm", trade_date)
    if use_cache and os.path.exists(cache_file):
        logger.info("Using cached CM bhavcopy for %s", trade_date)
        return pd.read_csv(cache_file)

    url = config.NSE_CM_BHAVCOPY_URL.format(date=trade_date)
    content = _download_with_retry(url)
    df = _extract_single_csv(content)
    _validate_columns(df, {"TckrSymb", "ClsPric", "TtlTradgVol"}, url)
    df.to_csv(cache_file, index=False)
    return df


def fetch_fo_bhavcopy(trade_date: date, use_cache: bool = True) -> pd.DataFrame:
    """Fetch NSE F&O bhavcopy (futures OI, settlement price, expiry) for one day."""
    cache_file = _cache_path("fo", trade_date)
    if use_cache and os.path.exists(cache_file):
        logger.info("Using cached FO bhavcopy for %s", trade_date)
        return pd.read_csv(cache_file)

    url = config.NSE_FO_BHAVCOPY_URL.format(date=trade_date)
    content = _download_with_retry(url)
    df = _extract_single_csv(content)
    _validate_columns(
        df, {"TckrSymb", "XpryDt", "FinInstrmTp", "OpnIntrst", "SttlmPric"}, url
    )
    df.to_csv(cache_file, index=False)
    return df


def _validate_columns(df: pd.DataFrame, required: set, source: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise NseFetchError(
            f"Bhavcopy schema from {source} is missing expected columns {missing}. "
            "NSE may have changed the file format — check "
            "https://www.nseindia.com/all-reports and update config.py."
        )
