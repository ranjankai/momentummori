"""
Configuration for the Momentum Tracker.

UNIVERSE_FILE: CSV of F&O-eligible symbols (one column: `symbol`).
A starter list is seeded in config/universe.csv. NSE revises the F&O
list quarterly — replace that file with the current list from
https://www.nseindia.com/content/fo/fo_mktlots.csv when you have
network access, or point UNIVERSE_FILE elsewhere.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UNIVERSE_FILE = os.path.join(BASE_DIR, "config", "universe.csv")

DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
LATEST_RESULT_FILE = os.path.join(DATA_DIR, "latest.json")

LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# NSE archive URL templates (UDiFF common bhavcopy format, current as of
# early 2026). NSE has changed this format before -- if fetches start
# failing with a parse error, check https://www.nseindia.com/all-reports
# for the current filename pattern and update these templates.
NSE_FO_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
)
NSE_CM_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
)

# NSE blocks requests without a browser-like User-Agent / referer.
NSE_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 2  # 2s, 4s, 8s

# Strategy parameters (mirrors the Altcase Momentum Leaders rules)
PORTFOLIO_SIZE = 10
MAX_SINGLE_STOCK_WEIGHT_PCT = 10
ROLLOVER_LOOKBACK_DAYS = 5       # trading days before expiry treated as "rollover window"
PRICE_MOMENTUM_LOOKBACK_DAYS = 63  # ~3 trading months
VOLUME_TREND_LOOKBACK_DAYS = 20

# Composite score weights (must sum to 1.0)
SIGNAL_WEIGHTS = {
    "rollover_pct": 0.35,
    "cost_of_carry": 0.25,
    "price_momentum": 0.25,
    "volume_trend": 0.15,
}
