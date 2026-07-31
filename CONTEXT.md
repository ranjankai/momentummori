# CONTEXT.md — Momentum Tracker

## Architecture Overview

Single-user, local-only Python app. No cloud services, no database.

- **Data source**: NSE bhavcopy ZIP/CSV files (cash-market + F&O),
  downloaded over HTTPS directly from `nsearchives.nseindia.com`.
- **Backend**: `app.py`, a stdlib `wsgiref` WSGI app (chosen over Flask
  because the build environment's package index was unreachable and
  Flask couldn't be installed or tested there — see below). Serves the
  dashboard and three JSON endpoints (`/api/rankings`, `/api/refresh`,
  `/api/status`).
- **Pipeline**: `pipeline.py` fetches ~3 months of cash bhavcopy plus
  one day of F&O bhavcopy, hands off to `scoring.py` for the four
  signals, caches the result to `data/latest.json`.
- **Frontend**: single static HTML/JS/CSS file (`static/index.html`),
  no build step, no framework.
- **Data flow**: browser → `/api/refresh` (POST) → `pipeline.run()` →
  `nse_client` (network + disk cache) → `scoring` (pandas) →
  `data/latest.json` → JSON response → table render.

## Deploy Command

There is no deployment — this runs on the user's own machine only.

```bash
pip install -r requirements.txt
python app.py
```

No CI/CD, no hosting, no remote git configured (personal single-file
tool; add a GitHub remote yourself if you want off-machine backup —
`git remote add origin <url> && git push -u origin main`).

## Manual Dashboard Config

None. All configuration lives in version-controlled files:

- `config.py` — NSE URL templates, request headers/timeouts/retries,
  strategy weights, portfolio size.
- `config/universe.csv` — the F&O stock universe to screen.

No env vars, no secrets, no external accounts required (NSE bhavcopy
archives are public, unauthenticated downloads).

## Knowledge Items (KIs)

- **NSE bhavcopy format churn**: NSE has changed its bhavcopy file
  naming and column schema multiple times (last known: "UDiFF common
  bhavcopy", `BhavCopy_NSE_{FO|CM}_0_0_0_YYYYMMDD_F_0000.csv.zip`,
  columns like `TckrSymb`, `XpryDt`, `OpnIntrst`, `SttlmPric`). If
  `nse_client.py` starts raising schema-validation errors, this is
  the first thing to check — see README "What's verified vs. not".
- **Sandbox network restriction**: the environment this was built in
  proxies all outbound HTTP through an allowlist that blocked
  `nseindia.com`, `finance.yahoo.com`, and even `google.com`. This is
  why live NSE fetching is implemented but untested in-session, and
  why the web server uses stdlib `wsgiref` instead of Flask (pip
  install to PyPI was also blocked). Not an issue on a normal machine
  with internet access.
- **Rollover % methodology**: computed as
  `next_month_OI / (near_month_OI + next_month_OI) * 100` using the
  two nearest unexpired FUTSTK contracts per symbol. This is the
  standard market convention; it's most informative in the last few
  days before monthly expiry (`config.ROLLOVER_LOOKBACK_DAYS`), which
  the current implementation does not yet filter on.
- **Composite scoring**: raw signals are on different scales (a %, an
  annualised %, a %, a ratio) so they're percentile-ranked across the
  universe before being weighted-summed — this avoids one signal's
  scale dominating the composite score.

## Pending Tasks

- Verify live NSE fetch on a machine with real internet access; fix
  URL/schema in `config.py` / `nse_client.py` if NSE has changed
  format since this was built (Jul 2026).
- Refresh `config/universe.csv` from NSE's official F&O eligibility
  list (currently a manually seeded starter list).
- Optional: add rollover-window filtering so rollover % is only
  emphasized near monthly expiry, matching the deck's methodology more
  precisely.
- Optional: set up a scheduled task to auto-refresh on trading days.
- Optional: initialize a remote git repo if off-machine backup is
  wanted (local repo only exists today).
