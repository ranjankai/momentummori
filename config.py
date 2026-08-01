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


# ---------------------------------------------------------------------------
# SECRETS
#
# Loaded from a gitignored .env next to this file. The real process
# environment ALWAYS wins, so Windows Task Scheduler (or any CI) can
# override without editing the file. Nothing here is ever committed.
# ---------------------------------------------------------------------------

ENV_FILE = os.path.join(BASE_DIR, ".env")


def _load_dotenv(path: str = None) -> None:
    """Minimal KEY=VALUE reader. Never overrides an existing env var."""
    path = path or ENV_FILE
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        # A missing or unreadable .env must never stop a strategy run.
        pass


_load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


# ---------------------------------------------------------------------------
# ALERTS (alerts.py)
# ---------------------------------------------------------------------------

ALERTS_ENABLED = True
ALERT_CHANNEL = "telegram"

# Send a short note when a run fails. Without this, a failed fetch is
# indistinguishable from "no trades today" -- both are silence.
ALERT_ON_FAILURE = True

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_PARSE_MODE = "HTML"      # Markdown breaks on symbols containing '_'
ALERT_MAX_CHARS = 4000            # Telegram hard limit is 4096


# ---------------------------------------------------------------------------
# GEMINI (llm.py)
#
# Waterfall: each model is tried in order until one returns valid JSON.
# All three were verified live on 01-Aug-2026 against a strict
# responseSchema with extended thinking enabled.
# ---------------------------------------------------------------------------

LLM_ENABLED = True
LLM_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
                "models/{model}:generateContent")

LLM_MODEL_WATERFALL = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

# "high" enables extended thinking. Flash-Lite has thinking OFF by default
# and only reasons when this is set -- verified 01-Aug-2026.
LLM_THINKING_LEVEL = "high"

LLM_TIMEOUT_SECONDS = 45
LLM_MAX_RETRIES = 3               # per model, before falling to the next tier
LLM_RETRY_BACKOFF_BASE_SECONDS = 2

# Responses are cached on disk by a hash of (prompt + schema), so a
# re-run of the same backtest costs nothing and returns identical values.
LLM_CACHE_DIR = os.path.join(DATA_DIR, "llm_cache")
LLM_CACHE_ENABLED = True


# ---------------------------------------------------------------------------
# NSE CORPORATE FEEDS (nse_corporate.py)
#
# Both verified reachable 01-Aug-2026. Note the NSE homepage returns 403
# while these API paths return 200 -- do NOT gate them on a homepage
# "session warm-up" call.
# ---------------------------------------------------------------------------

NSE_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&symbol={symbol}&from_date={from_date}&to_date={to_date}"
)
NSE_ASM_URL = "https://www.nseindia.com/api/reportASM"

# Days either side of the price move to search for an explanatory filing.
# Actions are usually filed on the ex-date itself; the back-window catches
# dividends and record-date entries filed a few days earlier.
CORP_ACTION_LOOKBACK_DAYS = 10
CORP_ACTION_LOOKAHEAD_DAYS = 3

CORP_CACHE_DIR = os.path.join(DATA_DIR, "corp_cache")
CORP_CACHE_ENABLED = True


# ---------------------------------------------------------------------------
# CORPORATE ACTION CLASSIFIER (corporate_actions.py)
#
# Gemini decides; there is no deterministic parser. The prompt states the
# reconciliation requirement explicitly. Code independently recomputes the
# residual between the model's adjustment_ratio and the observed price
# ratio -- purely as a REPORTED flag. It does not override the model.
# ---------------------------------------------------------------------------

CORP_ACTION_LLM_ENABLED = True

# |predicted / observed - 1| above this is reported as non-reconciling.
# 0.15 allows for genuine same-day price movement on top of the action
# (e.g. BSE's 2:1 bonus implies 0.3333 against an observed 0.3499).
CORP_ACTION_RECONCILE_TOLERANCE = 0.15

# What to do when the classifier cannot determine a ratio (UNKNOWN) --
# in practice, demergers, whose filed subject line carries no ratio.
#   "heuristic"  -> fall back to the legacy blind back-adjustment by the
#                   observed ratio. Preserves the behaviour the verified
#                   backtest table in CONTEXT.md was produced under.
#   "no_adjust"  -> leave the series alone and flag it.
# Neither is correct for a demerger: adjusting fully treats real value
# leaving the company as mechanics, not adjusting treats it as a trading
# loss. Default preserves comparability; flip it once you have decided.
CORP_ACTION_UNKNOWN_POLICY = "heuristic"


# Entry-sheet price band. The sheet is produced on expiry evening, before
# the next session's open is known, so entry prices are quoted as a band
# around the signal-day close rather than a single number.
ENTRY_BAND_PCT = 2.0

# Dynamic price band for F&O scrips: an order more than this far from the
# previous close is REJECTED by the exchange. Verified 01-Aug-2026: all
# 208 names in fo_mktlots.csv show "No Band" in NSE's sec_list.csv, i.e.
# no static circuit -- but the dynamic band still applies at order entry
# (SEBI/HO/MRD/TPD-1/P/CIR/2024/58, NSE/FAOP/64995).
#
# Consequence: the 40% target CANNOT be placed as a resting order on day
# one. It only becomes placeable once the stock is within this band of
# the target. The evening note says when.
PRICE_BAND_PCT = 10.0


# ---------------------------------------------------------------------------
# LEDGER
#
# Append-only history of every note sent: exits, orders issued, holdings
# and MTD, one JSON record per run. This is the audit trail -- what the
# system actually told you to do, on the day it told you, before you knew
# how it turned out. Deliberately NOT gitignored: it is history, not
# regenerable output.
#
# The rendered message is archived alongside it so you can see exactly
# what landed on your phone.
# ---------------------------------------------------------------------------

LEDGER_FILE = os.path.join(DATA_DIR, "ledger.jsonl")
LEDGER_ARCHIVE_DIR = os.path.join(DATA_DIR, "notes")
LEDGER_ENABLED = True


# ---------------------------------------------------------------------------
# PRE-ORDER SURVEILLANCE VETO (surveillance.py)
#
# Exclusion only -- it can drop a name from the basket, never promote one.
# NSE publishes no ASM history, so this CANNOT be backtested. It is
# adopted on reasoning, not evidence. Set VETO_ENABLED=False to disable.
# ---------------------------------------------------------------------------

VETO_ENABLED = True

# ASM stages that disqualify a name. Stage I is the mildest; higher stages
# carry 100% margin and trade-for-trade settlement, which makes the 5%
# resting stop unreliable.
VETO_ASM_STAGES = ("Stage I", "Stage II", "Stage III", "Stage IV")

# Veto short-term ASM as well as long-term.
VETO_INCLUDE_SHORTTERM_ASM = True
