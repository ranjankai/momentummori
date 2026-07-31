# Momentum Tracker

Personal-use web dashboard that screens NSE F&O stocks the way the
Altcase Momentum Leaders deck describes: derivatives rollover %, cost
of carry, price momentum and volume trend, combined into a composite
score, ranked, with the top 10 shown.

**This is a personal research tool, not investment advice.** It does
not place trades, hold your credentials, or guarantee data accuracy.

## What's verified vs. not

This was built in a sandboxed environment with no access to
`nseindia.com` (network is allowlisted and blocked it, along with
every other external site tested). So:

- **Verified in this session**: all scoring math (rollover %, cost of
  carry, price momentum, volume trend, composite ranking), the full
  pipeline wiring, and the web server's routes — all tested against
  synthetic data shaped like NSE's bhavcopy schema. See `tests/` and
  run `pytest` to reproduce.
- **Not verified**: whether the live NSE bhavcopy URLs in `config.py`
  and column names in `nse_client.py` are still correct today. NSE
  has changed this file format more than once. **The first time you
  run this on your own machine, check `logs/app.log` for a schema or
  404 error** — if you see one, open
  https://www.nseindia.com/all-reports, find the current bhavcopy file
  naming pattern, and update `NSE_FO_BHAVCOPY_URL` /
  `NSE_CM_BHAVCOPY_URL` in `config.py`.

## Setup

```bash
cd momentum-tracker
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Open http://127.0.0.1:5000, pick a date, click **Refresh**. The first
refresh downloads ~3 months of NSE cash-market bhavcopy files plus one
day of F&O bhavcopy, so it will take a while and print progress to the
console/`logs/app.log`. Subsequent refreshes reuse cached days from
`data/cache/`.

## Comparing against the Altcase deck's returns

The deck's page 6 discloses its return methodology: same capital
redeployed at the start of every month, each month's return "banked"
(not reinvested), headline number = simple sum of monthly returns
(not compounded). `backtest.py` / `run_backtest.py` reproduce that
*measurement* methodology so you can run it yourself:

```bash
python run_backtest.py --start 2025-04-01 --end 2026-04-30
```

**Do not expect this to reproduce the deck's +61.2% figure exactly.**
Altcase doesn't publish their exact composite formula, signal weights,
or rollover-window rules — only that the four signals exist. This
tool ranks stocks using `config.SIGNAL_WEIGHTS`, which are my own
reasonable defaults, not theirs. Also, the deck itself discloses that
the Apr–Dec 2025 leg is a hypothetical backtest "prepared with
hindsight." Use `run_backtest.py` to sanity-check the *shape* of the
result (does a similarly-built momentum strategy beat the benchmark
most months, with occasional sharp drawdowns) — not to reconcile the
exact number.

To compare against NIFTY 50 TRI, pass a benchmark CSV (index levels
aren't in NSE's equity bhavcopy, so there's no automatic fetch for
them — export one from niftyindices.com's historical data page):

```bash
python run_backtest.py --start 2025-04-01 --end 2026-04-30 --benchmark-csv nifty50_tri.csv
```

## Run the tests

```bash
pip install pytest
pytest
```

## How the score works

| Signal | Source | What it measures |
|---|---|---|
| Rollover % | F&O bhavcopy, near vs. next-month futures OI | Are positions being carried forward into next month? |
| Cost of carry | F&O bhavcopy settlement price vs. spot, annualised | Is real capital paying a premium to hold the position? |
| Price momentum | Cash bhavcopy, ~3-month return | Is the price actually trending up? |
| Volume trend | Cash bhavcopy, recent vs. earlier average volume | Is participation broadening? |

Each signal is percentile-ranked across the universe, then combined
with the weights in `config.SIGNAL_WEIGHTS` (rollover 35%, cost of
carry 25%, price momentum 25%, volume trend 15% by default — tune to
taste). Top 10 by composite score are shown, equal-weighted, mirroring
the deck's portfolio construction.

## Known gaps / next steps

- **Universe list** (`config/universe.csv`) is a manually seeded
  starter list of liquid NSE names, not the live F&O eligibility list.
  Replace it periodically from NSE's official list.
- **Rollover window**: the rollover % signal is most meaningful in the
  last few trading days before monthly expiry (see
  `config.ROLLOVER_LOOKBACK_DAYS`) — the current implementation
  computes it every day, which is fine for relative ranking but won't
  match Altcase's exact methodology away from expiry.
- **No scheduling**: you run this manually. If you want it to refresh
  automatically every trading day, ask to set that up as a scheduled
  task.
- **No portfolio/broker integration**: this only ranks stocks: it does
  not place, size, or track real trades.

## Project structure

```
momentum-tracker/
  app.py           # stdlib (wsgiref) web server, no Flask dependency
  pipeline.py       # orchestrates fetch -> score -> rank -> cache
  nse_client.py     # NSE bhavcopy download, retries, caching, logging
  scoring.py        # the four-signal math + composite ranking
  backtest.py       # deck-style accrued-return methodology (pure functions)
  run_backtest.py   # CLI to run a real backtest and compare vs benchmark
  config.py         # universe, URLs, strategy weights
  config/universe.csv
  static/index.html # dashboard UI
  tests/            # pytest suite (scoring + pipeline), all offline
  data/             # cache + latest result (gitignored)
  logs/             # rotating app.log (gitignored)
```
