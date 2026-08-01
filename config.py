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

# CSV of symbol,sector used to enforce the sector concentration cap
# (see MAX_SECTOR_WEIGHT_PCT below). Best-effort classification -- if a
# symbol is missing, it's treated as its own "Unclassified" bucket
# (still capped, just not grouped with anything). Missing entirely
# (file not found) degrades gracefully: no sector cap is applied.
SECTOR_MAP_FILE = os.path.join(BASE_DIR, "config", "sectors.csv")

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
MAX_SECTOR_WEIGHT_PCT = 30       # deck's disclosed cap -- enforced in scoring.rank_universe
ROLLOVER_LOOKBACK_DAYS = 5       # trading days before expiry treated as "rollover window"
PRICE_MOMENTUM_LOOKBACK_DAYS = 63  # ~3 trading months
VOLUME_TREND_LOOKBACK_DAYS = 20
VOLATILITY_LOOKBACK_DAYS = 63    # same window as price momentum, for the volatility signal

# Composite score weights (must sum to 1.0). low_volatility is a 5th
# signal added after backtesting showed this tool's worst drawdown
# months were both sector-concentrated AND running hotter (daily
# volatility) than the universe median -- exactly what the deck's
# disclosed "volatility-aware ranking" claims to filter for. Lower
# realized volatility scores higher here (see scoring.py).
SIGNAL_WEIGHTS = {
    "rollover_pct": 0.30,
    "cost_of_carry": 0.20,
    "price_momentum": 0.20,
    "volume_trend": 0.10,
    "low_volatility": 0.20,
}


# ---------------------------------------------------------------------------
# V4 STRATEGY (strategy.py) -- the configuration actually backtested.
#
# Selection is derivatives-informed but VOLATILITY-LED. That ordering was
# not a guess: across 13 months (Apr-2025..Apr-2026, 2677 stock-months),
# realised volatility was the only feature whose top decile contained
# meaningfully more than its share of winners (lift 1.5-2.0x). Rollover %
# on its own scored BELOW chance (lift 0.86). See CONTEXT.md.
#
# Every parameter here is fitted to 13 monthly observations. That is far
# too few to be significant (best t-stat achieved: ~1.2 against a ~2.0
# bar). Treat these as a starting hypothesis, not an established edge,
# until the history is extended -- see CONTEXT.md "Pending Tasks".
# ---------------------------------------------------------------------------

# Live NSE F&O eligibility list (UNDERLYING,SYMBOL,<lot size per month>).
# Download from https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv
FO_MKTLOTS_FILE = os.path.join(BASE_DIR, "fo_mktlots.csv")

V4_WEIGHTS = {
    "volatility": 0.50,   # 63d annualised SD of daily returns; HIGHER scores higher
    "rollover": 0.30,
    "cost_of_carry": 0.20,
}

V4_VOL_LOOKBACK_DAYS = 63
V4_HISTORY_DAYS = 260        # trading days of price history pulled per run

# Exit rules. The stop and target are resting broker orders, so they may
# trigger intra-day. Every other decision (entry, redeployment) happens
# once a day at the 9am open -- no discretionary intra-day trading.
V4_STOP_LOSS_PCT = 5.0
V4_TARGET_PCT = 40.0

# Redeployment: when a position exits mid-month its slot is refilled at the
# NEXT session's open with the highest-ranked eligible name (sector cap
# still enforced). Backtested 13-month accrued return, gross:
#   "never"          -> +30.79%   (a name, once sold, is not re-bought)
#   "not_if_stopped" -> +32.34%   (re-buy allowed unless it stopped you out)  <- default
#   "always"         -> +34.90%   (more return, but worst month -10.6% vs -6.5%)
#   redeployment off -> +20.65%
V4_REENTRY_POLICY = "not_if_stopped"

# Corporate actions: NSE bhavcopy close prices are NOT split/bonus adjusted.
# A day-on-day ratio outside these bounds is treated as a corporate action
# and the prior history is back-adjusted. Caught real 5:1 splits in MCX,
# KOTAKBANK, CAMS and ANGELONE that were otherwise scored as -80% crashes.
V4_SPLIT_RATIO_LOW = 0.6
V4_SPLIT_RATIO_HIGH = 1.8

# NSE moved monthly F&O expiry from the last Thursday to the last Tuesday
# with effect from 01-Sep-2025. Expiries landing on a holiday roll back to
# the previous trading day (see strategy.expiry_for).
EXPIRY_RULE_CHANGE = (2025, 9)      # (year, month) from which last-Tuesday applies
EXPIRY_WEEKDAY_BEFORE = 3           # Thursday
EXPIRY_WEEKDAY_AFTER = 1            # Tuesday

# Cross-month rebalancing ("v5"). When True, a stock that is still in the
# new month's capped basket is HELD across the rebalance -- no sell/rebuy
# round trip is recorded as a trade. It IS re-marked, though: at month-end
# (see strategy.simulate_month, ~line 525) the position is marked to that
# day's close, and BOTH cost basis and the 5%/40% stop/target band are
# re-derived off that new close -- not held from the original entry. A
# stock that drops out of the basket is sold at the new month's entry-day
# open exactly like a normal exit, and its slot is redeployed the next
# session. When False, every month starts from 10 empty slots (pre-v5
# behaviour, matching the verified backtest table in CONTEXT.md) -- kept
# as a rollback lever and for apples-to-apples comparison.
V4_CARRY_FORWARD = True

# Where cmd_basket persists currently-open positions between live runs, so
# it can tell HOLD from SELL from BUY the next time you generate a basket.
V4_HOLDINGS_FILE = os.path.join(DATA_DIR, "v4_holdings.json")
