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
- **Sandbox network egress is per-session, not global.** Verified
  01-Aug-2026: with Cowork's "Allow network egress" + domain allowlist
  set to "All domains" in settings, `nsearchives.nseindia.com` and
  `www.google.com` still returned `curl: (56) 403 from proxy after
  CONNECT` inside an *already-running* sandbox session. The setting
  only takes effect in a **newly started** session/conversation — user
  confirmed `nsearchives.nseindia.com` is reachable in a fresh sandbox.
  Takeaway: before assuming Cowork-side network access works (e.g. for
  a `mcp__scheduled-tasks` job hitting NSE), verify reachability
  *inside that specific session*, not by inference from settings or
  from another session's result.
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

## V4 Strategy (current — `strategy.py`, `run_strategy.py`)

The four-signal percentile model above is superseded for live use. V4 is
volatility-led:

    score = 0.50*z(realised vol 63d) + 0.30*z(rollover %) + 0.20*z(cost of carry)

- Snapshot on the monthly F&O **expiry** close; buy at the **next**
  session's open. Sell at the open after the following expiry.
- Top 10, equal weight, 30% sector cap (max 3 per sector).
- 5% stop-loss and 40% target as **resting broker orders** (these may
  fill intra-day). Every other decision happens once daily at the open.
- On exit, the slot is refilled the **next** session at the open with the
  highest-ranked available name. `V4_REENTRY_POLICY` controls whether a
  sold name can be re-bought.

### Verified backtest (Apr-2025 – Apr-2026, 13 months, gross)

| Variant | Accrued | Worst month | t-stat |
|---|---|---|---|
| No redeployment | +20.65% | -4.16% | 1.1 |
| Redeploy, `never` | +30.79% | -6.47% | 1.20 |
| Redeploy, `not_if_stopped` (default) | **+32.34%** | -6.47% | 1.22 |
| Redeploy, `always` | +34.90% | -10.58% | 1.10 |
| Buy & hold, no stops | +41.56% | -9.02% | 1.21 |
| F&O universe median | +8.11% | -10.27% | — |
| NIFTY 50 TRI (calendar, additive) | +6.39% | -10.19% | — |

Net of 0.35%/trade costs the default variant is roughly **+23%**.

### Knowledge Items — V4

- **Volatility is the signal; rollover is not.** Over 2677 stock-months,
  realised vol's top decile held 1.5–2.0x its share of winners; rollover %
  alone scored *below* chance (0.86x lift). This inverts the source deck's
  framing, which presents rollover as the lead signal.
- **Stops are fragile on volatile names.** Every trailing-stop variant
  collapses under realistic slippage (vol+coc: +1.5% → -11% at 1% slip)
  precisely because the selection targets stocks that gap. The 5% hard
  stop survives; trailing stops did not.
- **Simulate slots in lockstep, not sequentially.** An earlier version ran
  each slot's chain to month-end before starting the next. That let one
  slot claim a replacement another slot needed earlier in calendar time,
  and in re-entry mode let one stock occupy several slots at once.
  Inflated results by ~6.5pp (and ~40pp in the worst case).
- **Never fill on the signal day.** Redeployment buys at the *next*
  open — you cannot know at 09:15 that a stop will trigger at 14:00.
  An earlier version did, and it was worth a fake ~10pp.
- **Split adjustment is mandatory.** NSE bhavcopy is not corporate-action
  adjusted. MCX (5:1, 02-Jan-2026), KOTAKBANK, CAMS and ANGELONE all
  showed as ~-80% one-day crashes and scored as both worst-momentum and
  highest-volatility. `strategy.split_adjust` back-adjusts on a
  day-on-day ratio outside 0.6–1.8.
- **Expiry moved to the last Tuesday from 01-Sep-2025** (last Thursday
  before). Holidays roll *back* to the previous trading day — e.g.
  Mar-2026 expiry was the 30th, not the 31st (Mahavir Jayanti).
- **The source deck's benchmark is wrong.** It shows NIFTY 50 TRI at
  +5.8% for Mar-2026; official niftyindices.com data says **-10.19%**.
  Its live-period benchmark series appears shifted forward by one month
  (its Mar bar of 5.8 matches actual April's +5.84%). Its stated NIFTY
  total of +0.2% vs an actual +6.39% understates the benchmark by 6.2pp.
- **Sample size is the binding constraint.** 13 monthly observations;
  IC standard deviation ≈0.15, so detecting a 0.03 IC at t=2 needs ~100
  months. Best t-stat achieved anywhere in this work is ~1.2. Nothing
  here is statistically established.

## V5 Strategy (in progress — uncommitted)

V5 = V4 plus cross-month carry-forward. Currently **one confirmed change**,
implemented in `strategy.py` (`simulate_month`, `carry_forward` param) and
`config.py` (`V4_CARRY_FORWARD`), both uncommitted as of 01-Aug-2026:

- **Cross-month carry-forward** (`V4_CARRY_FORWARD = True`): a stock still
  in next month's capped top-10 is held across the rebalance instead of
  being force-sold and rebought — no round-trip trade recorded. But
  **contrary to `config.py`'s own comment, this is not "no reset"**:
  `strategy.py` (~line 525) marks the position to the final day's close
  and re-derives *both* cost basis and stop/target off that new close
  (`Position(sym, px_close, px_close*(1-stop_pct/100),
  px_close*(1+target_pct/100), final)`). So each month the stop/target
  band is fresh, anchored to the latest close, not the original entry.
  Verified directly against code 01-Aug-2026 — `config.py`'s inline
  comment is wrong and should be corrected there too. A stock that drops
  out of the basket is still sold at the new month's entry-day open, same
  as pre-v5. Set `V4_CARRY_FORWARD = False` for pre-v5 behaviour (matches
  the verified backtest table above) — kept as a rollback lever.

Two other changes were referenced in conversation (01-Aug-2026) but are
**not substantiated in code**:

- "Fixed day of entry and exit" — this describes *V4's* existing behaviour
  (snapshot at expiry close, buy next session's open, sell at the open
  after the following expiry), not a new V5 change. Unconfirmed whether
  V5 altered this further.
- A third change was referenced but never specified. Unavailable —
  needs to be supplied before it can be implemented or documented.

`V5_HOLDINGS_FILE` mechanism: `config.V4_HOLDINGS_FILE`
(`data/v4_holdings.json`) persists open positions between live runs so a
basket run can tell HOLD from SELL from BUY. Not yet backtested against
the V4 table above — no verified accrued-return numbers exist for V5 yet.

## Alerts Engine (planned — not yet built)

Goal: notify the user of BUY/SELL/HOLD actions from each day's V4/V5
basket run, without polling or a new pipeline.

- **Trigger point**: hook into `run_strategy.py`'s existing action list
  (the BUY/SELL/HOLD diff against `v4_holdings.json`, ~line 89) — do not
  build a separate signal-detection path.
- **Events**: BUY (new entry), SELL–stop hit, SELL–target hit,
  SELL–rollover (dropped from basket). HOLD-carried-forward alerts
  default OFF (likely too noisy).
- **Hard limitation**: this is EOD-only. Data source is daily bhavcopy,
  not a live broker feed, so a stop/target that fills intra-day is only
  known about at the *next* EOD run — same gap already noted under
  Broker Integration below. Not real-time until that's built.
- **New module**: `alerts.py` — `send_alert(event_type, symbol, details)`,
  wrapping the actual send in try/except with the same retry+backoff
  pattern `config.py` uses for NSE fetches (`RETRY_BACKOFF_BASE_SECONDS`).
  Every attempt (success/fail) logged to `logs/`. A failed alert must
  never crash or block the strategy run.
- **Config**: `config.py` gets `ALERTS_ENABLED`, `ALERT_CHANNEL`. Actual
  credentials (Telegram bot token / SMTP creds) come from environment
  variables — never hardcoded, per repo git hygiene rules.
- **Open decision**: delivery channel not yet chosen — Telegram bot
  (simpler, push to phone, no app-password setup) vs. email/SMTP (more
  familiar, easy to archive). Needs user decision before `alerts.py` is
  written.
- **Scheduling — architecture decision made 01-Aug-2026**: user wants
  this to run via a Cowork scheduled task (`mcp__scheduled-tasks`), not
  Windows Task Scheduler, *provided* the Cowork sandbox that executes it
  can reach `nsearchives.nseindia.com` (see network-egress KI above —
  must be verified in a fresh session before committing to this path).
  Fallback if Cowork sandbox egress doesn't hold up in practice: Windows
  Task Scheduler running `python run_strategy.py` locally, same as any
  other local cron use of this repo.

## Pending Tasks

- **Extend history to 2018.** The single highest-value task. `data/cache`
  starts Jan-2025, giving 13 monthly observations — far too few. NSE
  bhavcopy goes back years and `fetch_expiry_fo.py` generalises. ~85
  observations would make the IC tests capable of distinguishing signal
  from noise.
- **Broker integration (Zerodha Kite / ICICI Direct)** so the basket and
  its resting stop/target orders can be placed straight to the demat.
  Note: place stop and target as actual resting orders — the backtest
  assumes they fill intra-day without supervision.
- Re-download `fo_mktlots.csv` each quarter; NSE revises F&O eligibility
  monthly and the snapshot in the repo is point-in-time (31-Jul-2026).
  Using today's list for historical months is mild look-ahead bias:
  10 of 208 names had no F&O contract in Feb-2026.
- Add transaction costs and slippage directly into the backtest rather
  than applying them afterwards.
- Legacy: `config/universe.csv` (192 hand-seeded symbols) is superseded
  by `fo_mktlots.csv` for V4 but still used by `pipeline.py`.
- Optional: scheduled task to auto-generate the basket on expiry evening.
- Optional: remote git repo for off-machine backup (local only today).
- **Alerts engine** (see V5/Alerts sections above): pick delivery channel
  (Telegram vs. email), write `alerts.py`, hook into `run_strategy.py`'s
  action list, verify NSE reachability in a fresh Cowork sandbox before
  committing to a Cowork-scheduled-task approach over Task Scheduler.
- **V5 change #3**: unspecified by user as of 01-Aug-2026 — get this
  before finalizing V5's `strategy.py`/`config.py`.
- **Commit the uncommitted work.** As of 01-Aug-2026, `config.py`,
  `pipeline.py`, `run_backtest.py`, `scoring.py`, both test files, and
  `CONTEXT.md` are modified but uncommitted; `strategy.py`,
  `run_strategy.py`, `fo_mktlots.csv`, `config/sectors.csv`, and several
  `fetch_*_fo.py` scripts are untracked. This includes all of V4, V5's
  carry-forward change, and the sector-cap/volatility signal work —
  none of it is in git history yet. Needs a review pass and a commit
  (or several atomic ones) before it's safe to treat as a checkpoint.
