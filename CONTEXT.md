# CONTEXT.md — Momentum Tracker

## Working Rule — no hand-fixes to data files (added 27-Aug-2026)

Never hand-edit a live data file (`book.json`, `entry_tracking.json`, `cycle_state.json`, or any other state file) to fix a bug or correct a bad value. Before touching any of them:

1. **Exhaust programmatic options first.** Find or write a function that derives the correct value from real source data (the actual bhavcopy, the actual formula the live code uses) and apply it in code, then verify the result against an independent check (e.g. `entry_tracking.reconcile()`, a fresh call to the same function the live pipeline calls). Typing a number in — even a correctly-computed one — belongs in a script that derives and writes it, never in a manual JSON edit.
2. **Check with the user explicitly before applying any fix to live data**, even a programmatic one. Describe what's wrong, what the fix will change, and how it was derived, and wait for a go-ahead before writing.

Why this is a rule and not a suggestion: the 26-Aug KPITTECH/NATIONALUM frozen-quote incident was caused by a stale value nobody re-checked against current code. The fix for it (`reconcile()`/`apply_reconcile_fixes()`) was built specifically to close that gap — recompute fresh, diff against frozen, apply the diff's own output with no manual retyping. But the very first live use of that fix was itself applied by hand-editing `quote_price`/`shares`/`sl_price`/`exit_price` on `entry_tracking.json` directly, and `risk_anchor`/`sigma1` were never touched because nothing forced a review of the FULL record — only the fields that seemed relevant at the time. That gap sat dormant for a day and crashed the 27-Aug evening production run outright. Two hand-fixes, two different fields silently missed, same root cause both times: fixing a symptom by editing JSON instead of running code that derives the whole state.

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

## V5 Strategy (committed 01-Aug-2026)

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

## Runtime — how this actually runs (01-Aug-2026)

Everything below runs on the user's Windows machine. **Cowork plays no
part at runtime** — it was the build environment only.

Two scheduled entries in Windows Task Scheduler, both with *Start in* set
to the repo root (without it `.env` and `data/` do not resolve):

| when | command |
|---|---|
| weekdays 19:30 IST | `run_strategy.py daily` |
| expiry evening | `run_strategy.py sheet --expiry YYYY-MM-DD` then `perf` |

Chain for the nightly run:

1. Task Scheduler fires `.venv\Scripts\python.exe run_strategy.py daily`.
2. Trading-day guard: weekend or `.nodata` marker → exit silently, no
   message. Only real failures alert.
3. `nse_client` fetches the day's CM bhavcopy (disk cache first).
4. `strategy.load_price_history` assembles 260 trading days.
5. `strategy.compute_signals_cached` scores the universe; on a
   day-on-day ratio breach outside 0.6–1.8 it calls
   `corporate_actions.classify` → `nse_corporate` (NSE filings) →
   `llm` (Gemini) → cached verdict.
6. `daily_report.build` replays the month from the governing expiry and
   produces exits, orders and MTD.
7. `surveillance` drops any basket name currently under ASM.
8. `ledger.record` writes the audit trail **before** delivery.
9. `alerts.send` pushes to Telegram.

External dependencies at runtime: `nsearchives.nseindia.com` (bhavcopy),
`www.nseindia.com/api` (corporate actions, ASM),
`generativelanguage.googleapis.com` (Gemini, only on a ratio breach),
`api.telegram.org` (delivery). Every one of them fails soft.

## Alerts Engine (BUILT — 01-Aug-2026)

Telegram, not WhatsApp. WhatsApp was rejected: business-initiated
messages need Meta Business onboarding, a dedicated phone number, and
pre-approved templates. Telegram needed a BotFather token.

- `alerts.py` — `send(text)` and `send_failure(context, exc)`. Retry +
  backoff mirroring the NSE client, HTML parse mode, 4000-char chunking
  on line boundaries. Never raises; returns False and logs.
- Three messages: monthly order sheet (expiry evening), daily note
  (every trading evening), failure alert (on exception).
- Creds in a gitignored `.env`; real env vars take precedence.
- **Still EOD-only.** A stop that fills at 14:00 is invisible until the
  next evening's run. Not fixable without broker integration.

## Corporate Action Classifier (BUILT — 01-Aug-2026)

Replaces the blind assumption in `strategy.split_adjust` that any ratio
breach is a split. Gemini is given the price move plus every NSE filing
in a ±10/+3-day window and asked which combination explains it. There is
deliberately **no deterministic parser** — the hard cases are selection
and composition, not extraction.

- Waterfall `gemini-3.6-flash → 3.5-flash → 3.5-flash-lite`, extended
  thinking on all three, strict `responseSchema`, disk-cached by prompt
  hash so re-runs are free and byte-identical.
- **Scored 20/20** on the labelled set in `tests/labelled_breaches.py`,
  drawn from the real cache: SPLIT 8, BONUS 9, COMPOSITE 1, UNKNOWN 2.
- `CORP_ACTION_UNKNOWN_POLICY` (default `"heuristic"`) decides demerger
  handling. Neither option is correct — see KIs.

## Surveillance Veto (BUILT — 01-Aug-2026)

`surveillance.py` drops basket names under NSE ASM. Exclusion only,
never promotion. Fails open. **Cannot be backtested** — NSE publishes no
ASM history.

## Ledger (BUILT — 01-Aug-2026)

`data/ledger.jsonl` (one record per run) plus `data/notes/*.txt` (the
exact message sent). Written before delivery. **Tracked in git** — it is
history, not regenerable output. `run_strategy.py history` reads it back.

## Knowledge Items — 01-Aug-2026 session

- **A 40% target order cannot be placed in India.** F&O scrips carry a
  dynamic price band of ±10% of the previous close; anything outside is
  rejected at order entry, and when the band flexes the exchange cancels
  orders in the old band (SEBI/HO/MRD/TPD-1/P/CIR/2024/58, NSE/FAOP/64995).
  All 208 F&O names show `No Band` in NSE's `sec_list.csv` — that means
  no *static* circuit, not no constraint. **The target becomes placeable
  at roughly +27.3%** (1.40 / 1.10). The daily note says when.
  The backtest does NOT yet model this: it fills targets on any day whose
  high touches them. Two such fills exist in 13 months (POWERINDIA,
  ADANIGREEN), worth ~8pp of the +31.77% accrued.
- **The stop is modelled optimistically.** `low <= stop` books an exit AT
  the stop, but a stop-loss is a market order on trigger. Measured over
  80,132 stock-days: a 5% stop triggers on 2.89% of them, and **13.8% of
  those gap through**, median −1.84%, worst −30.03%. Mean drag across all
  triggers ≈ −0.39%.
- **Sandbox network egress is NOT blocked** (supersedes the earlier KI).
  Verified 01-Aug-2026 by fetching uncached 2024 bhavcopy. The stale
  warning in `nse_client.py`'s docstring is wrong and should be deleted.
- **NSE's homepage 403s while its APIs return 200.** Do not gate
  `/api/corporates-corporateActions` or `/api/reportASM` on a homepage
  cookie warm-up; it fails and buys nothing.
- **A 404 from NSE is permanent, not transient.** It was being retried
  3× with 2/4/8s backoff, costing 14s per market holiday. Now raised
  immediately as `NseNoDataError` and negative-cached. Report build time
  went from 120s+ to 17.7s.
- **ASM barely intersects the F&O universe** — 1 of 208 on 01-Aug-2026.
  ASM/GSM police illiquid small caps; F&O eligibility screens those out
  by construction. The veto still fired on KALYANKJIL in the live basket,
  so it is not useless, but its expected hit rate is very low.
- **pandas 3.0 requires Python ≥3.11**, which the Cowork sandbox (3.10)
  cannot satisfy. Windows runs 3.13.14 / pandas 3.0.5; `requirements.txt`
  is pinned to that. The 24-test suite passes on both 2.3.3 and 3.0.5.
- **Verified backtest under the current code: +31.77%** over the same 13
  months, against +32.34% previously — a −0.57pp change. The classifier
  barely moved the number here; its value is insurance against a future
  genuine collapse being laundered into a clean series, not alpha.
  t-stat 1.30, 8/13 months positive, worst −8.01%.

## Walk-forward test, 02-Aug-2026 — THE LLM TARGET LAYER LOST

Five cycles, real rupees, Rs100 start, Rs10 per slot, compounding inside
each slot. Baskets from the live V4 ranking. Targets set by hand from the
53-field payload with tickers anonymised.

| cycle | LLM target, 10% SL | flat 40%, 10% SL | flat 40%, 5% SL |
|---|---|---|---|
| Dec-25 -> Jan-26 | -3.04% | -4.88% | -3.28% |
| Jan-26 -> Feb-26 | +12.60% | +14.20% | +13.77% |
| Feb-26 -> Mar-26 | -12.66% | -7.87% | **-3.37%** |
| Mar-26 -> Apr-26 | +10.66% | **+19.17%** | +10.03% |
| Apr-26 -> May-26 | -0.98% | -1.49% | -0.93% |
| **Rs100 ->** | **104.49** | **117.49** | **115.90** |

**The configuration that existed before 02-Aug-2026 beat everything built
that day, by 13 percentage points over five months.**

Why the LLM targets lost: they cap winners. In Mar-Apr eight positions
were booked at 10-14%; left alone at 40% the same names ran to +19.17%.
Momentum strategies cannot cap the upside -- 2 target hits in 13 months
is the DESIGN, not a defect. The flat 40% is not there to be hit often,
it is there to not get in the way.

Tested whether the missing daily off-momentum judgement explained the
gap. It did not. Mar-Apr re-run day by day, judging each holding on that
day's data only: **+9.73%**, i.e. 0.93pp WORSE than targets alone. The
single off-momentum exit fired sold IDEA at +0.7%; that same position
went on to hit its +10% target three weeks later. Selling a resting
stock is exactly what the prompt warns against, and the prompt did not
prevent it.

### Knowledge Items — 02-Aug-2026

- **STOP-LOSS REGIME SPLIT. The strongest effect measured all day.**
  Feb-Mar (market -12.5%): 5% stop -3.37%, 10% stop -12.66%.
  Mar-Apr (rally): 5% stop +10.03%, 10% stop +19.17%.
  Tight stops win in falling months, wide stops win in rising months,
  ~9pp in each direction. Bigger than the entire LLM layer. THIS is the
  next thing to work on -- see Pending Tasks.
- **A 5% stop triggers on 2.88% of stock-days, a 10% stop on 0.19%**
  (80,337 stock-days, corporate actions excluded). 93% fewer stop-outs
  at 10% -- but Feb-2026 shows that is not automatically better.
- **13.8% of 5%-stop triggers GAP THROUGH the stop**, median -1.84%,
  worst -30.03%. A stop is a market order; the backtest fills it at the
  trigger price and so overstates.
- **F&O dynamic price band is +/-10% of the previous close.** A 40%
  target CANNOT be placed as a resting order until the stock is up
  ~27.3% (1.40/1.10). All 208 F&O names show "No Band" in NSE's
  sec_list.csv -- that means no STATIC circuit, not no constraint. The
  backtest assumes the target is always resting and therefore overstates
  target fills. UNRESOLVED, and the one finding from 02-Aug that stands.
- **Rollover is FROZEN between expiries.** Measured over the Jul-2026
  cycle: rank correlation with the previous snapshot 0.84-0.95 for three
  weeks, then breaks in the last ~4 sessions. Mid-cycle rollover has
  -0.1 rank correlation with the expiry value -- unrelated, not a weak
  version. Derivatives are unusable for any mid-month decision. This is
  why ROLLOVER_LOOKBACK_DAYS exists.
- **Feb-2026 was a market crash, not a strategy failure.** F&O universe
  median -12.50%, 93% of 207 names negative. Our -12.66% tracked it.
- **A manually-run comparison basket was FLAT (+0.03%) that same month.**
  Zero overlap with ours; their picks ranked 42nd-197th in our universe.
  Median 63d volatility: theirs 28.2 (= universe median 27.5), ours 45.9.
  Their stops ~4.5%, targets ~9%, three of which hit. We invert the
  deck's "volatility-aware ranking" -- we buy the MOST volatile names.
  February is what that costs in a down month.
- **Redeployment is roughly a wash** (flat 40%, 10% SL): Dec -4.88 vs
  -5.84, Jan +14.20 vs +13.03, Feb -7.87 vs -7.30. Helps slightly when
  falling, hurts when rising -- the replacement is by then a weaker name.
- **NSE publishes a FORWARD holiday feed**
  (/api/holiday-master?type=trading). known_trading_days() is inferred
  from cached bhavcopy and cannot holiday-check a FUTURE expiry.
  Nov-2026 is the live case: last Tuesday is 24-Nov, an F&O holiday, so
  expiry is Mon 23-Nov.
- **Liquidity floor added** (MIN_TURNOVER_CRORE). Measured: median
  Rs 183cr, min Rs 34cr, zero breaches -- F&O eligibility already
  screens hard, but the check now exists rather than being claimed.
- **Additive return understates reality.** Feb-Mar: -11.43% additive vs
  -12.66% actual rupees. After a slot loses, the replacement is bought
  with the reduced capital, so its gains are on a smaller base.

### Architecture changes, 02-Aug-2026

- **One simulator.** daily_report carried a second copy of every trading
  rule plus its own Position/Exit classes. simulate_month now returns
  open_positions/exits/to_buy with ORIGINAL cost bases, snapshotted
  before the carry-forward branch re-marks them. Found in the process:
  carry_forward=False force-sells at the final day's OPEN and understated
  cycle return by 0.30pp; a mid-month view must mark to the CLOSE.
- **Backtest REMOVED from the live CLI** (legacy/backtest_cmd.py). It was
  the only caller driving a second code path.
- **legacy/** holds app.py, pipeline.py, backtest.py, run_backtest.py,
  static/ and their tests. Live suite 14 tests, legacy suite 10.
- Four LLM call sites exist: corporate_actions.classify (KEEP -- 20/20
  on the labelled set, the only one with evidence), llm_judgment
  target_for / exit_judgement / choose_candidate (all three UNPROVEN and
  measured NEGATIVE above).

## LIVE CONFIGURATION as of 02-Aug-2026 (end of session)

```
REGIME_STOP_ENABLED      True    breadth >=45% -> 5% stop, <45% -> 10%
V4_TARGET_PCT            40.0    unchanged
V4_REDEPLOY_ENABLED      False   freed slots hold cash to expiry
V4_CARRY_FORWARD         True
LLM_TARGET_ENABLED       False
LLM_EXIT_ENABLED         False
LLM_CANDIDATE_ENABLED    False
CORP_ACTION_LLM_ENABLED  True    the only LLM call kept
VETO_ENABLED             True
```

Turning the LLM flags off is sufficient -- with them False, `llm_judgment`
is never imported, `choose_candidate` is never called and
`next_candidate` returns None immediately. The modules are retained
deliberately: they hold four prompts and a 20/20 validation set, and
deleting them would mean unpicking the simulator refactor that removed
the duplicate engine.

## REGIME-PEGGED STOP -- the one change adopted

The stop is chosen ONCE, on the expiry close, from market breadth:
the percentage of the F&O universe trading above its own 200-day average.

| cycle | breadth | 5% stop | 10% stop | better |
|---|---|---|---|---|
| 2025-12 | 55.1% | -3.28% | -4.88% | 5% |
| 2026-01 | 40.0% | +13.77% | +14.20% | 10% |
| 2026-02 | 48.1% | -3.37% | **-12.66%** | 5% |
| 2026-03 | 19.4% | +10.03% | **+19.17%** | 10% |
| 2026-04 | 49.0% | -0.93% | -1.49% | 5% |

Breadth separated the better stop **5/5** at a 45% threshold. Median
universe volatility separated **0/5** -- the intuitive version of this
idea does not work. Compounded: always-5% 115.90, always-10% 113.99,
breadth-pegged **126.28**.

Logic: a beaten-down market (low breadth) is one you are buying the
bounce in, so a wide stop avoids being shaken out; a healthy-looking
market's risk is a sudden crash, so cut fast. Feb-2026 is the case --
breadth 48%, median 20-day return POSITIVE going in, then -12.5%.

**FIVE OBSERVATIONS, threshold chosen after seeing the outcomes.** A
coin-flip rule separates 5 points about 1 time in 10. This is a
hypothesis under live test. `REGIME_STOP_ENABLED = False` reverts.

Live breadth on the 28-Jul-2026 expiry: 50.0% -> 5% stop.

## Reverse-engineering the comparison portfolio (02-Aug-2026)

A manually-run monthly basket ("top 10 scrips monthly.xlsx", sheets
DEC 2025 / FEB 2026 / MAR 2026) returned +6.40, +6.81, +0.03, +9.23,
+6.29 across five cycles -- positive in all five, including the month the
F&O median fell 12.5%. Compounded Rs100 -> 131.98 against our 115.90.

### Their selection model, inferred (17/28 reproduced, 12.6x lift)

```
FILTERS   Close > 20 DMA,  Close > 50 DMA,  Cash Volume Rank <= 150
SCORE     Trend Composite = 0.5*(60D rank) + 0.3*(20D rank) + 0.2*(5D rank)
BUILD     max 2 per sector, take 10
```
Trend Composite AUC **0.927**; selected median rank 24 vs 107. Both-DMA
alone keeps 89% of their picks in 29% of the universe.

### What the analysis RULED OUT -- all of it measured, none of it guessed

- **Derivatives contribute nothing to their selection.** OI Rank AUC
  0.500 p=0.995. Roll Surprise 0.524 p=0.70. Roll Z 0.553 p=0.57.
  Current Roll 0.428 p=0.21. Delivery % 0.456 p=0.46. The
  rollover-surprise hypothesis is not weak, it is ABSENT.
- **Volatility / trend quality is not the missing signal.** Only ATR20
  Rank was significant (d=-1.06, p=0.026) and adding it moved 17->19
  only at exactly 0.25 weight, collapsing to 13 at 0.5 and 11 at 1.0.
  Trend R-squared went the WRONG way -- their picks have LOWER trend
  quality than our false positives.
- **Size / index membership is not it either.** SizeTier AUC 0.487
  p=0.744. N100 membership AUC 0.482 p=0.715, and as a filter it HALVES
  the hit rate (15 -> 9). They are not running a large-cap universe.
- **Nothing beat noise.** Calibrated against 200 random features at the
  same weight: P(noise >= +2) = 1.5%. The three best real candidates all
  scored exactly +2 -- including OI Rank, which has AUC 0.500 by
  construction. With 18 candidates tested, that is chance.

### THE ACTUAL DIFFERENCE: entry price, not selection

Their sheet quotes an `Entry level` per stock. Against the market:

```
entry vs selection-day CLOSE   median -1.15%   below close 23/29
entry vs NEXT-DAY OPEN         median -2.10%   below open  27/29
```

**27 of 29 entries are below the next day's open.** They place limit
orders under the market and fill on dips -- NATIONALUM at 358 against a
396 open (-9.7%), ONGC 268.50 vs 290 (-7.4%), DMART 3730 vs 4075 (-8.5%).
We buy all ten at the open. Their exits are also modest and reachable:
target median **+9.7%**, stop median **-4.7%**.

Caveat: their sheets show only the ten that FILLED. Names selected whose
limit never filled are invisible, so their published returns are
conditional on fills and flatter the strategy by an unknown amount.

### Copying their selection does NOT reproduce their returns

```
Rs100, five cycles
  trend selection + our exits      102.13
  trend selection + THEIR exits     99.04
  our live V4                      115.90
  THEM                             131.98
```
We reproduce ~60% of their picks and earn Rs102. Selection is not where
their edge is.

## Four selection hypotheses tested against RETURNS -- all failed

| change | Rs100 over 5 cycles |
|---|---|
| LLM per-stock targets | 104.49 |
| volatility centring (Gaussian at universe median) | 103.75 |
| price-momentum selection | 102.13 |
| sector-first selection (best of 5 variants) | 107.85 |
| **live V4 (unchanged)** | **115.90** |

Notes: the Gaussian reproduced their volatility profile almost exactly
(25.9 vs 28.2, 33.3 vs 32.9) and still shared only 2 of 49 picks --
matching a summary statistic says nothing about selection. Sector-first
got WORSE the more it concentrated (3 sectors 98.65, 10 sectors 107.85),
so its only benefit was diversification, not sector choice.

## Why the LLM target layer failed, precisely

The prompt asked for a level "reachable in 21 sessions" and filled the
context with caution language -- resistance, extension, drawdown. Every
instruction pushed the answer DOWN, and nothing said that setting it too
low permanently forfeits the upside. Mar-Apr 2026: eight positions booked
at 10-14% while the same names finished at +12.9, +12.7, +11.3, +19.3,
+30.0, +50.5, +50.6. **7 of 8 kept running; 1 fell back.**

The flat 40% is not a profit-booking rule, it is a TAIL-RISK CAP. Firing
twice in thirteen months is the design working. A tight target is
incompatible with a high-volatility selection: you can have one or the
other.

A reframed prompt ("predict the PEAK over the next N days", drop if
<1%, cap at 40%) was tested on the clean Apr-May cycle: -0.42% vs -0.93%
for flat 40%. The +0.51pp came from ONE fill. Predictions were too high
in 9 of 10 cases (median predicted 12.5%, median actual peak 4.1%).
Safer than the old framing, but not established.

## New data sources wired 02-Aug-2026

- `nse_client.fetch_delivery_data(date)` -- NSE `sec_bhavdata_full`
  (DDMMYYYY, plain CSV). Adds DELIV_QTY, DELIV_PER, NO_OF_TRADES,
  AVG_PRICE. Same cache/retry/.nodata semantics.
- F&O volume columns were ALREADY mapped in `scoring.normalize_fo_columns`
  (`volume`, `turnover`, `change_in_oi`) and simply unused.
- NIFTY 50/100/200/500 constituent lists fetch fine from
  `nsearchives.nseindia.com/content/indices/ind_niftyNNNlist.csv`.
- `tools_features.py <expiry>` builds the seven-sheet feature workbook
  for any expiry, resolving the three prior expiries itself.

## VERIFIED 13-MONTH BACKTEST -- final configuration (02-Aug-2026)

Run through `strategy.simulate_month` (the SAME function daily_report
calls), via `tools_run13.py`. Carry-forward ON, redeployment OFF,
regime-pegged stop, 40% target.

| cycle | breadth | stop | return | trades | carried |
|---|---|---|---|---|---|
| 2025-03 | 27.3% | 10% | +6.01% | 4 | 6 |
| 2025-04 | 45.5% | 5% | -0.08% | 9 | 1 |
| 2025-05 | 54.0% | 5% | +0.60% | 5 | 5 |
| 2025-06 | 63.3% | 5% | -3.42% | 10 | 0 |
| 2025-07 | 56.8% | 5% | -3.98% | 8 | 2 |
| 2025-08 | 50.0% | 5% | +3.95% | 3 | 7 |
| 2025-09 | 55.4% | 5% | +3.41% | 3 | 7 |
| 2025-10 | 71.2% | 5% | +0.81% | 7 | 3 |
| 2025-11 | 59.0% | 5% | +2.81% | 6 | 4 |
| 2025-12 | 55.1% | 5% | -2.26% | 9 | 1 |
| 2026-01 | 40.0% | 10% | +14.23% | 1 | 9 |
| 2026-02 | 48.1% | 5% | -2.22% | 9 | 1 |
| 2026-03 | 19.4% | 10% | +19.47% | 2 | 8 |

```
ACCRUED (sum)      +39.34%      vs +31.77% for the old config
COMPOUNDED Rs100    143.60
worst month         -3.98%      vs -8.01% -- HALVED
max drawdown        -7.27%
positive months        8/13
mean 3.03%/mo | sd 6.91 | t-stat 1.58   (was ~1.30)
F&O universe median over the same window: +8.66%
```

The regime stop is worth ~7.6pp. Carry-forward on/off is ~0.7pp -- noise
at this sample size; it helps in down months (Dec +1.02, Feb +1.15) and
hurts in up months (Oct -1.56) because a carried position is re-marked to
the month-end close and its stop re-derived off that higher basis.

The wide stop fired in only 3 of 13 cycles (Mar-25, Jan-26, Mar-26) and
was right in all three. Note the trades column: wide-stop months run 1-4
trades, tight-stop months 7-10. The mechanism is not "wider stop earns
more" -- it is "wider stop stops you being shaken out, so carry-forward
has something left to carry."

### METHOD WARNING -- read before trusting any afternoon number

Several comparisons run on 02-Aug-2026 used an ad-hoc `sim()` helper that
reimplemented the monthly loop and **silently dropped carry-forward and
chaining**. Anything quoting "live V4 = Rs115.90" came from that helper,
not from the real engine. The RELATIVE rankings in those tests are
probably still valid (both arms shared the construction) but the LEVELS
are not comparable to the table above. `tools_run13.py` is the only
13-month result produced by the production code path -- use it.

## Session 03-Aug-2026 -- exit rules and selection rules, both rejected

### Accounting convention FIXED

Backtests are **fresh-start and additive**. Rs100 is deployed on the first
session after each expiry and fully closed at the first session after the
next expiry. Monthly returns are SUMMED, never compounded, because
investors enter and exit at will and each month's number must belong to
whoever was invested that month.

Carry-forward is an **operational detail for the Telegram messages only**
-- it decides whether a held name generates a SELL+BUY pair or nothing.
It is NOT a backtest assumption. Earlier tables that leaned on chained
carry-forward are superseded by the matrix below.

### The 2x2 matrix -- 13 cycles, fresh start, additive (`tools_matrix.py`)

| entry | exit | sum | mean | sd | worst | best | pos | t |
|---|---|---|---|---|---|---|---|---|
| **V4** | **V4** | **+34.02%** | 2.62% | 7.47 | -6.18% | 19.84% | 8/13 | 1.26 |
| V4 | ATR SOP | +18.66% | 1.44% | 7.92 | -6.09% | 21.89% | 7/13 | 0.65 |
| v1.1 | V4 | +9.09% | 0.70% | 3.79 | -5.64% | 5.80% | 8/13 | 0.66 |
| v1.1 | ATR SOP | +0.33% | 0.03% | 3.49 | -4.53% | 5.19% | 6/13 | 0.03 |

Attribution: swapping the exit costs -15.36pp, swapping the entry costs
-24.93pp, swapping both costs -33.69pp. **Nothing beat the live config.**

### Why the ATR conviction SOP failed

Stops and targets as ATR20 multiples by conviction rank (1-3: 2.5x/6x,
4-7: 2.0x/5x, 8-10: 1.5x/4x); the target only promotes INITIAL -> WINNER,
then a ratchet stop trails the high and never widens.

The promotion gate never opens. Median target distance is **18.05%** in a
~34-day window, so only **21 of 130 positions** ever reached WINNER. The
trailing stop -- the entire point of the design -- fired 17 times in 130.
Adding carry-forward made it WORSE (+16.99% vs +18.66%): a carried
position keeps its original frozen entry, so after a bad month the target
sits even further overhead.

Conviction buckets showed no monotonic payoff: Top 3 mean -0.19%,
Middle 4 +4.07%, Bottom 3 -0.45%. Rank 1-3 did no better than rank 8-10,
so the tiered ATR multiples are not earning anything.

### Why the v1.1 selection rule failed

Equal-weight blend of a derivative block (roll surprise, carry, OI change,
futures volume), a volatility block that rewards CALM, and a trend
composite; mandatory close > 20 DMA and > 50 DMA; max 3 per sector; top 10
of a top-50 candidate pool.

Two causes, both structural:

1. **The volatility term is inverted relative to what works.** V4 scores
   HIGH volatility higher; v1.1 rewards calm. The two biggest months --
   Jan-26 (+13.70%) and Mar-26 (+19.84%) -- returned +0.83% and +5.80%
   under v1.1. sd halves (7.47 -> 3.79) and so does the return: it cuts
   the right tail, not the risk.
2. **The DMA gates bind hardest when they cost most.** Mar-2026 breadth
   19.4% left only **7 names** in the whole F&O universe above both DMAs,
   so v1.1 produced a 7-stock portfolio. Jul-25 and Aug-25 ran at 42 and
   41. The filter empties the pool right after a crash, which is when the
   rebound happens.

### Altcase overlap -- v1.1 matches better and earns less

Across 12 months and 115 mapped names: **v1.1 overlap 24 (20.9%)**,
**V4 overlap 8 (7.0%)**. v1.1 triples the name-matching and returns
+0.33% against V4's +34.02%.

This is the cleanest confirmation yet that **their edge is entry price,
not stock choice** (27/29 entries below the next-day open, median -2.10%).
Getting closer to their basket does not get you closer to their returns.

### Mar-2026 hand-picked basket, both exit rules (`tools_cycle_compare.py`)

PREMIERENE, WAAREEENER, MCX, NATIONALUM, COALINDIA, DMART, ONGC,
AUROPHARMA, ADANIPOWER, PERSISTENT -- entered 31-Mar open, closed 29-Apr
open. **V4 +11.33%, ATR SOP +9.92%.**

The 10% regime stop held every position to rollover; nothing stopped out.
ADANIPOWER was the only name to touch the 40% target (24-Apr, +38.99%)
and the only SOP promotion to WINNER, trailed out at +38.45% -- 0.54pp
worse than simply selling at the target. The gap came from COALINDIA and
AUROPHARMA, whose tighter ATR stops threw them out before they recovered.

Note: 7 of these 10 are exactly what v1.1 selected that month; the other
three (MCX, ADANIPOWER, PERSISTENT) are the Step-8 fills, and they roughly
doubled the month.

### Tooling added 03-Aug-2026

- `tools_enrich_features.py` -- adds a 34-column `0 Master` sheet plus
  `8 Universe Filters` and `9 Notes` to any feature workbook: 6M rollover
  baseline, OI build-up classification, ASM/GSM, T2T, date-exact F&O ban,
  NIFTY 50/100/200 membership.
- `tools_matrix.py` -- the 2x2 entry/exit backtest above.
- `tools_sop13.py`, `tools_sop_run.py`, `tools_cycle_compare.py` -- ATR
  SOP walk-forward and single-cycle comparison.

DATA GAPS, unresolved: **Market Cap and Free Float Market Cap have no
accessible source.** NSE's `quote-equity` API (which carries `issuedSize`)
returns 403 and the archive weightage files 404. Columns ship empty.
**ASM/GSM and NIFTY membership are CURRENT snapshots**, not archived per
date, so applying them historically leaks look-ahead -- they are NOT used
in the backtest. The F&O ban list IS date-exact and IS applied. T2T is
structurally impossible inside F&O, so that filter is a no-op.

CAUTION: `tools_cycle_compare.py` originally loaded 90 days of history,
which made `market_breadth` return `nan` and silently fall back to the
5% stop. Any helper calling `market_breadth` or `resolve_stop_pct` must
load the FULL history window -- the 200-DMA needs 200 sessions.

## 03-Aug-2026 evening -- three production bugs fixed

### 1. Daily report crashed for the whole first half of every month

`daily_report.governing_expiry` asked `expiry_for` to holiday-adjust the
CURRENT month's expiry before checking whether that expiry had happened.
The roll-back can only find days already in `known_trading_days()`, which
is inferred from cached bhavcopy -- the past. On 03-Aug it looked at
25-Aug, walked back 10 days, found nothing, and raised. Fixed by
resolving the raw weekday first (no calendar, cannot fail) to decide the
cycle, then applying roll-back only to the returned past expiry. The
10-day window preserves the holiday case (31-Mar-2026 Mahavir Jayanti ->
30-Mar). Verified across the rule change and both holiday shapes.

Root cause worth remembering: this DUPLICATED `run_strategy.resolve_expiry`,
which already solved the same problem correctly via the NSE holiday feed.
The duplicate is the one that broke.

### 2. Corporate actions were never applied to the holding window

`split_adjust` only ever cleaned the volatility lookback. The OHLC that
positions are walked against was raw. BSE's 2:1 on 23-05-2025 read as a
-61.99% crash, tripped the stop, and cost the Apr-2025 cycle 6.2pp. LIVE,
the same event fires a spurious EXIT alert on a stock that merely split.
New `strategy.adjust_holding_window` rescales every bar from the ex-date,
which is what a broker's GTT adjustment achieves. Returns the input
untouched when nothing breaches, so the normal-case cost is one scan.

LIMIT: triggers on a one-day ratio outside [0.72, 1.40], so a 5:4 bonus
slips through. It should call `corporate_actions.classify` instead of
using a fixed band.

### 3. Stops could not lose more than their width

`simulate_month` filled at `pos.stop` on any intraday touch. Correct for
a resting order that trades AT the level -- wrong when the session GAPS
through it, which fills at the open. TRENT closed 3343.80 on 06-07-2026
and opened 3080.00 against a 3120.75 stop: a -6.24% fill, not -5.00%.
Same logic mirrored for targets (gap-up fills better than the target).

### THE canonical 13-cycle number: +39.57%

`tools_run13.py` was RETIRED 03-Aug-2026. It chained carry-forward across
expiries, a convention superseded when the reporting basis was fixed as
fresh-start and additive. Two conventions producing two numbers is how
+36.87% briefly became the headline on the wrong basis.

Canonical runner: **`research/run13.py`**, which calls
`strategy.simulate_month` through `research/harness.py`.

| cycle | breadth | stop | return |
|---|---|---|---|
| 2025-03 | 27.3 | 10 | +4.97% |
| 2025-04 | 45.5 | 5 | +0.28% |
| 2025-05 | 54.0 | 5 | +1.50% |
| 2025-06 | 63.3 | 5 | -3.20% |
| 2025-07 | 56.8 | 5 | -4.25% |
| 2025-08 | 50.0 | 5 | +4.47% |
| 2025-09 | 55.4 | 5 | +4.29% |
| 2025-10 | 71.2 | 5 | +2.46% |
| 2025-11 | 59.0 | 5 | +3.38% |
| 2025-12 | 55.1 | 5 | -3.51% |
| 2026-01 | 40.0 | 10 | +13.61% |
| 2026-02 | 48.1 | 5 | -4.02% |
| 2026-03 | 19.4 | 10 | +19.61% |

**sum +39.57% | mean 3.04%/mo | sd 7.01 | worst -4.25% | positive 9/13 |
t 1.57**

For reference only, the retired carry-forward convention gave +36.87%
after the same fixes (+39.34% before them). Do not mix the two.

Cross-check worth recording: the deleted `tools_stopsweep.py` produced
+39.57% for this convention too. The research code was arithmetically
right; the problem was that it could not be TRUSTED without being checked
against the engine. That is the whole argument for `harness.py`.

### Telegram outage 02-Aug to 03-Aug

The group was upgraded to a supergroup, which permanently re-issues the
chat ID (basic groups have short negative IDs; supergroups start -100).
Telegram returned 400 with `migrate_to_chat_id` and the bot posted to a
dead address for two days. `alerts.py` now reads that field and retries
once against the new ID, logging a loud instruction to update `.env`.

STILL MISSING: nothing alarms when a trading day passes with no delivery.
The failure was found by the user noticing, not by the system.

### research/ quarantined

Nine scripts that reimplemented the position walk instead of calling
`simulate_month` were moved to `research/` with a README. Same defect as
the old `sim()` helper. Every comparison run on 03-Aug (the 2x2 matrix,
the ATR SOP, the v1.1 selector, the Altcase gap, the stop sweep, the
breadth de-risk sweep, the overlap matching) came from those files and is
UNVERIFIED until re-run through `simulate_month`.

Rule going forward: new backtests call `strategy.simulate_month`. If it
lacks a parameter, add the parameter -- do not fork the loop.

## Session 04-Aug to 07-Aug-2026 Updates

### 1. Transient NSE 404 Caching Fix (`nse_client.py`)
- **Problem**: Early daytime runs before NSE published the day's bhavcopy (around 18:00 IST) received HTTP 404 and cached a permanent `.nodata` marker. Subsequent evening runs skipped network fetches, treating valid trading days as non-trading days.
- **Fix**: Implemented `NODATA_GRACE_DAYS = 4`. `404 Not Found` markers on dates within the grace period are treated as transient (not yet published) and are automatically deleted on subsequent retries rather than poisoning the date permanently.

### 2. Exact Rupee Stop Loss Output in Daily Notes (`daily_report.py`)
- **Enhancement**: Holdings trading below entry price now display their exact rupee stop loss level and percentage distance in daily alerts (e.g., `IDEA  12.86  (-1.2%)  --  SL 12.37 (3.8% away)`), followed by a directive to verify the resting broker order at that exact price.

### 3. Dynamic Limit Entry Bands & Minimum Portfolio Sizing (`daily_report.py`, `config.py`)
- **Dynamic Limit Bands**: Entry sheets scale stock limit order entry ranges using 20-day daily price ranges (`_compute_stock_entry_band`), bounded by `ENTRY_BAND_MIN_PCT` (1.5%) and `ENTRY_BAND_MAX_PCT` (6.0%).
- **Minimum Portfolio Guide**: `render_entry_sheet` includes whole-share quantity recommendations and a Minimum Portfolio Guide (`_compute_min_portfolio_sizing`) to assist users in sizing equal-weighted baskets without severe rounding distortion.

## Session 08-Aug to 15-Aug-2026 — carry-forward redesign, 3-stage fills restored, message split

Large session covering the whole carry-forward/rebalance redesign and a
full rewrite of the expiry-evening Telegram messages. Full derivations,
concrete before/after numbers, and validation runs live in
`BACKTEST_LOG.md` (new file this session) — this section is the map, not
the detail.

### 1. Corporate-action fix in the carry-forward backtest
`strategy.adjust_holding_window` gained `return_factors=True`, exposed
through `simulate_month` as `MonthResult.corp_action_factors`.
`research/carry_forward_v5.py`'s whole-share book ledger applies the
factor to real share counts on a split/bonus (the missed 2025-05-23 BSE
split was the trigger) and switched to RAW (unadjusted) prices for
valuation. See `BACKTEST_LOG.md` for the fix and the two-pass correction
needed after the first attempt over-corrected price alongside shares.

### 2. Coverage-scale + never-sell-holds redesign (the core rebuild)
Explicit instruction: a HELD name is never sold to bring its weight down
— only a genuine stop/target/rollover exit sells it. New slot-target
algorithm, implemented identically in the research script and in
production (`daily_report.py`'s `build_entry_sheet`/
`_compute_hold_rebalance`):
1. Build a fresh min-basket exactly as a brand-new investor gets.
2. Coverage-scale `k = max(held_shares / that_basket's_own_share_count)`
   over every hold, using the RATIO not the raw share count.
3. Buy-feasibility floor, generalised to ALL names (not just fresh buys):
   `slot_target = max(scaled_slot, max(entry_lo for every row))`.
4. Resolve whole shares for the FULL basket against that slot.
5. A HOLD needing more shares gets an unconditional Day-1 MARKET top-up
   (no limit chase, no gap-abort — a top-up isn't new exposure).

`book.py` (new file) persists real per-symbol share counts across
months — the piece that made "currently held N, target M" comparisons
possible at all; before this nothing survived a position past its first
month.

### 3. "v6" self-contained monthly P&L + New-investor basket
Monthly return = this-cycle-only booked P&L (exit proceeds + month-end
mark of continuing holds, minus this-cycle's own entry cost: holds at
real Day-1 price, fresh buys at real fill price) / this-cycle's min
basket. Deliberately NOT NAV-differenced against the prior month.
New-investor basket = Existing's real per-symbol shares scaled by
`1 / min_shares_in_existing`, `max(1, round(shares*scale))` per symbol —
this is what gives New and Existing the *same per-symbol entry-price
basis*, which matters for the message design below.

### 4. entry_tracking.py: 3-stage fill mechanism, restored
A 2-stage collapse (Day-1 limit / Day-2 mandatory) went in and out the
same evening (13/14-Aug) without actually being agreed — reverted back
to the original, validated 3-stage design:
- **Day 1**: 20-day vol-band LIMIT, whole-share sizing off the priciest
  name's own band-low × N slots.
- **Day 2** (Day-1 misses only): a NEW limit, re-priced off Day-1's own
  realized volatility (80%-probability Parkinson estimate). Still a real
  limit order, NOT mandatory yet.
- **Day 3** (Day-2 misses only): pools Day-1+Day-2 vol, decides share
  count the evening before, executes unconditionally at Day-3's actual
  open — the basket must be complete.
- Risk anchor (stop/target) is always Day-1's actual open, regardless of
  which stage actually fills. Gap-risk abort checked before each stage.
- `render()` splits "still open" into `pending_limit` (a real order to
  place) vs `pending_mandatory` (informational only, fills automatically)
  — conflating the two under one "MANDATORY" header was the 2-stage
  version's actual bug.

**Continuing (HOLD-tagged) names get a `market_buy` flag** through
`open_window(..., market_buy_symbols=...)`: they skip the whole 3-stage
chain and fill unconditionally at Day-1's market open, same as an
existing investor's top-up — needed so a NEW investor's entry price on
those names sits on the same basis as an existing investor's (explicit
instruction, 15-Aug-2026).

**Day-3→Stream-2 cutover fix**: previously waited one extra "+1" evening
after resolution to repeat an already-final message unchanged (noise
when everything resolved early, e.g. by Day 2). `cmd_daily` now marks
`final_sent` the SAME evening `resolved_as_of` gets set, so the normal
daily note (`cycle_state`/Stream 2) resumes the very next session,
whichever day resolution actually landed on — verified both for an
early (Day-2) and a full (Day-3 mandatory) resolution.

### 5. Three expiry-evening messages, redesigned and wired into `cmd_sheet`
Exactly 3 messages, in order (explicit instruction — earlier drafts
wrongly assumed a 4th):
1. **Performance scorecard** (`render_performance`, unchanged).
2. **New-investor message** (`entry_tracking.render_new_investor_day0`,
   new) — numbered list, "INR"-prefixed, every slot a fresh entry
   (HOLD-tagged names print as "@ market open" and are listed last, not
   inline). Opened over the FULL basket with `market_buy_symbols` set to
   the HOLD-tagged names.
3. **Existing-investor message** (`daily_report.render_entry_sheet`,
   rewritten) — same numbered/INR style, SELL → TOP-UP → BUY in one
   continuous numbering, SL/Exit line placed before TOP-UP (not SELL,
   which doesn't carry one), no P&L on SELL lines (the scorecard already
   covers that), no per-stock dev%, no "MINIMUM PORTFOLIO GUIDE" bullet
   block, no SL-placement/target-band reminder text, no "Surveillance
   check did not run" line (internal detail, not investor-facing).
   Header: `Entry portfolio for <Mon>-<Mon> '<YY> - Existing investors`.
4. Day 1 onward is IDENTICAL for both audiences — one shared
   `entry_tracking.render()`, header changed to
   `<StartMonth>-<EndMon> '<YY> PORTFOLIO - <date>`. Verified this
   degrades correctly (no crash, numerically identical BUY lists) in the
   no-held-stocks case, where `k` naturally defaults to `1.0`.

All of the above verified end-to-end by calling the real `cmd_sheet`/
`cmd_daily` functions (not reimplementations) against isolated `/tmp`
paths for `book.BOOK_FILE`, `entry_tracking.STATE_FILE`,
`config.LEDGER_FILE`/`LEDGER_ARCHIVE_DIR` — 58/58 unit tests pass.

### Incident: production data touched by a dry-run test (15-Aug-2026)
A `run_strategy.py sheet --no-send` verification run was executed
against the REAL `data/` paths instead of isolated ones. `--no-send`
only blocks Telegram delivery — it still writes `data/entry_tracking.
json`, appends to `data/ledger.jsonl`, and writes `data/notes/*.txt`.
Real `data/entry_tracking.json` got overwritten with fake pending state
(`resolved_as_of: null, final_sent: false`), which would have made the
next real scheduled run wrongly re-open Stream 1. Fixed by reconstructing
the file from the untouched `data/cycle_state.json` (real entry prices,
all filled Day 1, `final_sent: true`) and verifying `is_window_active()`
returns `False` again. 3 stray `ledger.jsonl` lines removed (confirmed
via `written_at` timestamp — the file is append-only, so this was a safe
truncation, not a rewrite). 3 new `data/notes/2026-07-28_*.txt` files
couldn't be deleted (this sandbox cannot unlink files on this mount) so
were overwritten with an explicit "TEST ARTIFACT" disclaimer instead.
`data/book.json` and `data/cycle_state.json` were never touched.
**Lesson: `--no-send` is not a safe flag for dry-running against real
data — it only suppresses delivery, not the file writes. Use isolated
paths for every one of `book.BOOK_FILE`, `entry_tracking.STATE_FILE`,
`cycle_state.STATE_FILE`, `config.LEDGER_FILE`/`LEDGER_ARCHIVE_DIR`
before invoking any real CLI command against test data.**

## Session 17-Aug-2026 — isolated exit-ratchet test, also rejected

Follow-up to the 03-Aug-2026 ATR conviction SOP rejection above. That
test confounded three things (conviction-tiered ATR stops, a promotion
gate, and a ratchet) and lost -15.36pp; it was never a clean test of the
simplest version of the idea: keep the live 5%(regime)/40% stop and
target exactly as they are, and ONLY add a ratchet -- once a session's
HIGH reaches entry+trigger%, raise the stop (never lower it) to
entry+lock%, nothing else touched.

Implemented as two new optional params on `strategy.simulate_month`
(`ratchet_trigger_pct`, `ratchet_lock_pct`, both `None` by default --
every existing caller, including production, is byte-for-byte
unaffected) and a matching pass-through on `research/harness.run_cycle`.
Swept 9 trigger/lock combinations (15/0, 15/5, 20/0, 20/8, 20/12, 25/10,
25/15, 30/15, 30/20) across the same canonical 13 cycles (Mar-2025 ..
Mar-2026), classifier OFF for determinism -- the classifier-OFF baseline
reproduced the canonical classifier-ON +39.57% exactly, so the sweep is
directly comparable, no caveat needed. See
`research/exit_ratchet_experiment.py`.

**Result: no configuration beat the unmodified baseline.** Configs where
the ratchet never actually fired (trigger ≥20% with a large enough lock)
matched +39.57% exactly, by definition. Every config where it DID fire
came in lower — as low as +34.38% (20% trigger / 12% lock). The worst
month also got WORSE, not better, in every config that fired (Feb-2026
went from -4.02% to -5.44% at 15%-trigger/breakeven-lock) — the direct
opposite of the "temper the losses" hypothesis this was testing.

**Mechanism, isolated with a concrete example (NATIONALUM, Feb-2026
cycle):** entered at 347.00, ran up past +15% intraday at some point,
never came close to threatening the original -5% stop, and was still
open at month-end, marked to 399.45 (+15.12%) in the baseline — a
perfectly good, unrealised-but-real month. With a 15%-trigger/breakeven
ratchet, the stop got raised to 347.00 the session after it crossed
+15%, the stock then drifted back down through breakeven on 23-Mar and
got stopped there at exactly 0%. The ratchet did not save this position
from a crash — there wasn't one — it just cashed out a stock that was
still going to be fine, and because `V4_REDEPLOY_ENABLED` is `False` in
production, the freed slot then sat in cash for the rest of the month
instead of earning the +15.12% it would otherwise have kept. That is the
whole effect, repeated across every case the ratchet fired: **an early
exit only pays off if the alternative was a stop-out at a WORSE price
than the ratchet level, and in this basket, positions that pulled back
after a run mostly kept drifting rather than crashing — so the ratchet's
"protection" was hardly ever needed, and its cost (giving up the
remaining drift, then earning nothing on the freed capital) was paid
every time regardless.**

This is a NEGATIVE result, consistent with the 03-Aug-2026 finding, now
shown to hold even with the confounds removed. Not pursuing a
trailing/ratchet exit further unless `V4_REDEPLOY_ENABLED` changes (an
early exit into a live redeploy, rather than into cash, would change the
cost side of this trade-off entirely — untested, out of scope here).

## Session 21-Aug-2026 — live daily note was silently missing its SOLD section

Caught by asking "should we not show a SOLD bucket after the hold one?".
`render()`'s "Exited" section (`rpt.exited_review`, mark-to-today for
already-sold names) and "SELL ORDERS" section (`rpt.sell_orders`,
target-placeable reminders) both existed and both worked -- but only if
fed by `daily_report.build()`, which computed them. `cmd_daily` has used
`cycle_state.build()` for the live evening note since the incremental
rewrite (see the WHY section at the top of `cycle_state.py`), and its
`to_report()` never carried that port over. `daily_report.build()` is
dead code now (nothing calls it), so both sections have been silently
empty in every live message since the rewrite.

Fixed in `cycle_state.py`: `apply_session` now refreshes `last_close` on
an EXITED position too (skips the stop/target/corp-action re-check,
just the price mark -- costs nothing extra, the frame is already in
hand), since the module's "EXITED is terminal" design otherwise froze
the price on exit day, making a same-day-forward `now_pct` comparison
impossible. `to_report()` now populates `sell_orders` and
`exited_review` from `holdings`/`exits`, mirroring the old
`daily_report.build()` logic (no LLM off-momentum leg -- that needs the
full day's price history, which the incremental path deliberately
doesn't load).

`daily_report.render()`: exit date now shown with an ordinal day (`"on
18th"`) rather than bare, and the "← left on the table" flag on exited
names was removed per explicit instruction -- it's a closed trade with
no action possible; framing it as money left on the table read as
second-guessing a decision that's already done.

Verified against a read-only copy of the real `data/cycle_state.json`
(ADANIGREEN, stopped out -5.0% on 18-Aug, now showing -5.4% in the
Exited section, right after CONTINUE TO HOLD). 58/58 tests pass.

## Session 25-Aug-2026 — expiry-evening incident, Apr-Aug performance backfill, book.json reconstruction

**The incident.** Tonight's real `cmd_sheet` run started (logged "Opened
entry-tracking window") and then died with no further log line, no
failure alert, no messages sent. Cause: `entry_tracking.
render_new_investor_day0` used `expiry_dt.strftime("%-d %B %Y")` --
`%-d` (no leading zero) is a Linux/glibc strftime extension, not
supported on Windows, where this actually runs. Raised `ValueError`
uncaught, because this whole block (`entry_tracking.open_window()`
through the ledger writes) sat outside `cmd_sheet`'s try/except -- only
`build_entry_sheet`/`render_entry_sheet` were protected. Fixed the date
formatting (`f"{expiry_dt.day} {expiry_dt.strftime('%B %Y')}"`,
portable) and wrapped the rest of `cmd_sheet` in its own try/except so a
future failure here reports instead of vanishing.

**Numbering and month-label fixes**, `daily_report.render_entry_sheet`/
`render_performance`: SELL/TOP-UP/BUY/CONTINUE-TO-HOLD each now number
independently from 1 (previously one continuous count across all
sections, which was confusing -- "9. KALYANKJIL" under a heading with
only 6 items). CONTINUE TO HOLD rows are now numbered too (previously
bare). Performance-message month labels were off by one cycle: they
used the raw expiry month instead of the HOLDING month (expiry + 1) --
a 30-Jun-2026 expiry's money sits in the market through JULY (entry_date
the next session), so it belongs on the "Jul" row, not "Jun". Every
other date label in this codebase (`_cycle_title`,
`_entry_portfolio_title`) already used the correct convention; this one
didn't.

**Apr-Aug performance backfill.** The live ledger only has "daily"
records from the June-2026 and July-2026 expiries onward (live tracking
started then) -- so the performance message could only ever show 2
months, when investors were told it would show Apr onward. Backfilled
March/April/May-2026 expiries (holding Apr/May/Jun) into the real
`data/ledger.jsonl` via `research/harness.run_cycle` -- the same
production engine as the canonical 13-cycle number, NOT a
reimplementation. Flagged each backfilled row's `flagged_actions` with
an explicit "BACKFILLED" note. Caveat, stated in that note too:
`harness.v4_basket` does not apply the ASM surveillance veto (no
historical ASM snapshot exists), so these three months' basket
composition is an approximation of what live production would actually
have held. Performance message now reads Apr +19.61%, May -0.80%,
Jun -0.30%, Jul -3.77%, Aug +4.29% (in progress), total +19.03%.

**White paper cross-check.** Re-ran the real canonical 13-cycle backtest
(`research/run13.py`) from scratch to verify the numbers quoted in the
white paper delivered earlier this session. 12 of 13 cycles matched
exactly. One did not: 2025-04 (Apr-2025 expiry, held into May-2025) --
white paper says +0.28%, fresh re-run gives +0.81%, deterministically
(3/3 identical re-runs). Traced to BSE Ltd (in that cycle's basket),
which had a real 2:1 bonus, ex-date 23-May-2025, inside the holding
window -- exactly the grey-zone corporate-action case the classifier
exists to catch, and `harness.py`'s own docstring already documents that
a classifier-ON backtest "can in principle differ if the model answers
differently" run to run. Net: total moves from +39.57% to +40.11%
(0.54pp), everything else (worst month, best month, win rate,
methodology) checks out exactly.

**book.json reconstruction for the 4 pre-book.py holdings.** TRENT,
BANDHANBNK, POWERINDIA, GVT&D (continuing HOLD names in tonight's real
sheet) had no book record -- they were bought 29-Jul-2026, before
book.py existed, and nothing ever backfilled it. Per explicit
instruction ("assume you started as a new investor in Mar-Apr cycle and
accordingly build the existing investor portfolio"), reconstructed via
`research/book_backfill.py`: walks the exact same mechanical chain
`build_entry_sheet` uses every real month (`_compute_min_portfolio_
sizing` -> coverage-scale k -> `_resolve_shares_to_target` ->
`_compute_hold_rebalance`), cycle by cycle from the Mar-2026 expiry
through the REAL, known 28-Jul-2026 basket (not backtested -- ground
truth from this session's earlier `entry_tracking.json` reconstruction).
Same no-historical-ASM-veto caveat as above applies to the Mar/Apr/
May/Jun-2026 legs. Result: TRENT and GVT&D trace a real accumulation
(entered fresh in the Jun-2026 expiry cycle, one coverage-scale top-up
into Jul-2026) -- shares 16 and 11 respectively as of 28-Jul-2026.
BANDHANBNK and POWERINDIA do not appear in the no-veto backtest's
Apr/May/Jun-2026 baskets at all (POWERINDIA drops out after Mar-2026;
BANDHANBNK only appears in the May-2026-expiry basket, not June's), so
under this reconstruction they come out as fresh 28-Jul-2026 entries
(276 and 2 shares) rather than a multi-cycle accumulation -- explicitly
flagged as such, not silently guessed. If the real (ASM-vetoed) basket
actually held either of them continuously earlier, this would
understate their true accumulated count; unverifiable without a
historical ASM archive, which does not exist. Seeded into the real
`data/book.json`. Verified end-to-end against an isolated copy of
tonight's real sheet: TOP-UP correctly shows "TRENT: Add 1 more share
(already held: 16)" etc. instead of the "no book record on file"
fallback.

**Reconstruction REVERTED same night -- it corrupted an unrelated
stock's own correct number.** Once seeded, tonight's New-investor
message showed POWERINDIA at 2 shares. Before the reconstruction (no
book record at all, coverage-scale k defaulting to 1.0), the same
message correctly showed "POWERINDIA: 1 share @ market open" -- which
is POWERINDIA's own, by-construction, 0%-deviation anchor fit (it is
the priciest name in the basket; `base_slot` is defined off its own
band-low). Cause: TRENT/GVT&D/BANDHANBNK's reconstructed ratios pushed
coverage-scale k to ~1.556, inflating the ONE shared slot used to solve
every row's share count. At that inflated slot, `_resolve_shares_to_
target`'s deviation-minimization flips POWERINDIA from n=1 (36.49%
under at the inflated slot) to n=2 (22.03% over) -- numerically closer,
but wrong, because POWERINDIA never needed scaling; only the OTHER
names' reconstructed accumulation did. This is exactly the same
unverifiable data the reconstruction script's own docstring already
flagged ("cannot verify a multi-cycle history... falls back to treating
as fresh entry") going on to corrupt a different, previously-correct
number. Reverted `data/book.json` to `{}`. Re-verified: POWERINDIA back
to the correct 1 share, min basket back to a clean, un-inflated
~INR 324,792, no distortion anywhere else. Conclusion: the plain "no
book record on file, please check against the slot target above"
fallback was never a defect -- it's the honest, safe default for data
we can't verify, and it was already producing the correct downstream
math. Do not re-seed `book.json` from a backtest-style reconstruction
again; if TRENT/BANDHANBNK/POWERINDIA/GVT&D's real historical share
counts are ever needed, get them from the user's actual broker
statement, not a reconstructed backtest chain.

**Real fix, same night: coverage-scale k retired entirely** (explicit
instruction -- "no chutiye, WAIT... 1) FIND NEW MIN BASKET 2) mark
continuing names BUY AT MARKET OPEN for new guys 3) for existing guys,
just show the min number they need to continue holding or if they need
to buy more"). The k-inflation mechanism itself, not just the
reconstructed data feeding it, was the bug: it let one hold's
accumulated ratio inflate the ONE shared slot every row is solved
against, corrupting unrelated names' own correct fit. `build_entry_
sheet` now always uses `slot_target = base_slot` -- the fresh,
un-inflated `_compute_min_portfolio_sizing` result, exactly what a
brand-new investor gets, every cycle, no ratio, no scaling.
`_compute_hold_rebalance` unchanged in mechanism (still top-up-only,
never sells) but now solves against this plain slot, so its `n` really
is the cycle's true minimum. Re-seeded `data/book.json` with the
reconstructed values (16/276/2/11) since the corruption path that made
them unsafe is now closed -- verified TRENT/BANDHANBNK/POWERINDIA/
GVT&D all correctly land on their own natural minimum (11/186/1/8)
with no cross-contamination.

**CONTINUE TO HOLD display, corrected again same night**: first pass
showed "hold your {actual held} shares (min basket needs {min})" --
wrong; the line must show the min basket number plainly, same as every
other line in the sheet, not the investor's actual held count (that
was the whole point of "we equalized the new and existing basket
completely" -- one canonical number, not a per-investor variant).
Fixed to `"{i}. {sym}: hold {min_shares} shares"`. "upscale as needed"
already covers investors holding more than the minimum; the message
doesn't need to say so per line.

Final verified real sheet for 25-Aug-2026 (all three messages, generated
from the real production code against the real `book.json`, run in an
isolated copy so nothing was sent/written until confirmed):
- Min basket: ~INR 324,792, same figure in both New and Existing
  investor messages.
- New investors: 10 lines, POWERINDIA correctly at 1 share.
- Existing investors: SELL (KAYNES, FORCEMOT, SAIL, IDEA, AMBER), BUY
  (6 fresh limit orders), CONTINUE TO HOLD -- TRENT 11, BANDHANBNK 186,
  POWERINDIA 1, GVT&D 8.

## Session 25-Aug-2026 (continued) -- limit-price-above-close bug, then a
## full audit and architecture fix (book.json as single source of truth)

**`_solve_shares_to_slot` tie-break bug, real and kept.** User noticed
KPITTECH quoted at 601.47 when it closed at 592 -- a real "limit above
market" defeats the point of a limit order. Root cause: when multiple
whole-share counts land inside a stock's volatility band with ~equal
(near-zero) deviation from the target slot, the old code kept whichever
candidate Python's `set` iterated first -- arbitrary, unrelated to
price. Fixed: on a tie (within 1e-6%), prefer the lower price.

**A same-day close-cap was ALSO tried and REVERTED, on evidence.**
First instinct was "never quote above close, full stop" -- clamping the
upper bound to `close` instead of `entry_hi`. This is NOT the same bug:
a genuinely unique best fit (no tie, e.g. COFORGE) landing slightly
above close is legitimate -- `entry_hi` is a real, volatility-derived
estimate of where the stock can plausibly trade, not decoration.
Measured via a new backtest (`research/fill_realism_v6_3stage.py`, the
real 3-stage mechanism ported from entry_tracking.py's live code and
run against the canonical 13-cycle basket): the close-cap dropped the
Day-1 fill rate from 61% to 37% (everything that misses Day 1 falls to
Day 2's much less precise Parkinson re-quote) and cost -2.6pt of real
return over 13 cycles. Reverted the cap, kept the tie-break. With tie-
break only: Day-1 fill rate 48.5%, real 3-stage cost -0.91pt over 13
cycles (+40.11% idealized -> +39.20% real) -- a small, sane,
explainable "implementation shortfall" (Perold 1988), not an outlier.
Lesson stated plainly for next time: a fix driven by "this looks
stupid" needs to be measured against the real mechanism before being
trusted, not just pattern-matched to the complaint.

**Discovery: `cycle_state.py` (live daily/monthly tracking) and
`daily_report.build()` (still-live, NOT dead code despite this file's
own 21-Aug-2026 section claiming otherwise -- it's called from
`build_entry_sheet` line ~625 to reconstruct the outgoing month) both
independently assumed every position enters at "the open of the first
session after expiry," completely ignoring that `entry_tracking.py` +
`book.py` already track the REAL fill price/day via the 3-stage
mechanism. Two facts forced this to the surface: (1) re-deriving
Apr/May/Jun-2026's backfilled performance through the real 3-stage
mechanism showed real, non-trivial swings (some months better, some
worse, never zero) versus the idealized number actually sent; (2) user
asked directly why book.json wasn't the obvious single source of truth
here. It should have been, and now is.

**Full audit, three parallel agents (strategy.py/corporate_actions.py,
daily_report.py, ledger.py/run_strategy.py/config.py), findings
independently spot-verified before acting.** Fixed the same session,
"keep it simple, no new architecture" (explicit instruction -- rejected
the idea of a second `ACTUAL_FILLS_FILE`-style parallel mechanism;
book.json already existed and just needed to be read):

1. **book.py rewritten as the actual single source of truth.**
   `open_position` now also stores `risk_anchor` (previously only lived
   transiently in entry_tracking.json, never persisted -- needed because
   stop/target are deliberately computed off Day-1's open, never a
   delayed Day-2/3 fill price, per the 12-Aug-2026 "risk-anchor fix").
   New `seed_pending(symbols, expiry)`, called from
   `entry_tracking.open_window`, gives the book a full 10-name row at
   expiry evening instead of only gaining rows lazily as fills happen
   (explicit instruction: "book.json should have 10 names with blank
   entry prices on Day of expiry evening"). `close_position` now
   ARCHIVES the full record to `data/book_archive.jsonl` (append-only)
   before deleting from the live book, instead of just deleting --
   explicit instruction: "when book_close happens, the data should be
   archived so we don't simulate everything again" (this is exactly why
   every backtest tonight needed caveats about ASM being unreconstructable
   for a past date -- a real archive going forward removes that need for
   anything closed from here on). `run_strategy.py`'s two close_position
   call sites now pass exit_price/exit_date/reason.
2. **cycle_state.py's PENDING->HOLD transition** now reads
   `entry_price`/`risk_anchor` from `book.get(sym)` when a real record
   exists (it always should, by the time this runs -- `cmd_daily` gates
   on `entry_tracking.is_window_active` first, so the window has fully
   resolved), falling back to today's open only if no book record
   exists at all (legacy/pre-book.py edge case).
3. **daily_report.build()** now builds `entry_overrides` from
   `book.json` before calling `strategy.simulate_month` -- reusing the
   EXISTING `entry_overrides` parameter (built for the fill-realism
   research), not a new mechanism. Entry dates at or before the
   reconstruction window's own first day (a continuing multi-cycle hold,
   or a backfilled record stamped with the expiry date itself) are
   clamped to `days[0]` -- unclamped, `simulate_month`'s day-by-day loop
   would defer to a date it never visits and the position would
   silently never open. Verified against tonight's real sheet before
   and after: identical, correct output.
4. **daily_report.build_entry_sheet's silent exception fallback
   removed.** Previously caught ANY failure reconstructing the outgoing
   month and continued with `current={}` (every name renders as a fresh
   BUY, doubling exposure on continuing holds, zero SELLs) -- just a
   `logger.warning`, no alert. Now propagates to `cmd_sheet`'s existing
   try/except, which does alert. A degraded-but-successful-looking sheet
   is worse than a loud failure.
5. **cmd_daily's entry-tracking branch (Days 1-3 after every expiry)**
   was unprotected by try/except -- the exact same blind spot as the
   `%-d` incident, just a different code path that hadn't failed yet.
   Wrapped, matching cmd_sheet's pattern.
6. **`render()` now surfaces `rpt.flagged_actions`** (e.g. "no price in
   today's bhavcopy -- stop not evaluated") -- previously written to the
   ledger via `ledger.record` but never actually shown in any message a
   human sees.
7. **cmd_basket persisted the flat `config.V4_STOP_LOSS_PCT`** into its
   printed summary and the `basket_*.json` audit file, even though the
   basket's real stop (shown in its own table) is `decision.stop_pct`,
   the regime-pegged value when `REGIME_STOP_ENABLED`. Fixed to use
   `decision.stop_pct` in both places.
8. **`market_breadth` (the regime-stop signal) read raw, un-split-
   adjusted closes** -- confirmed by direct code read, no adjustment
   call anywhere in the function, while the selection signal and
   holding P&L both already adjust. A split/bonus in the trailing ~200
   sessions biased that stock's "above/below 200-day average" reading
   for months, and several logged cycles sit within a few points of the
   45% regime threshold. Fixed with `split_adjust`'s legacy heuristic
   (no symbol/dates/classifier -- this runs across the whole ~200-symbol
   universe every cycle, same cost concern as item 9 below).
9. **`CORP_ACTION_GREY_ZONE_ENABLED` was `True`**, directly contradicting
   its own comment ("OFF because it... makes the daily report time
   out... turn on only with a capped symbol list") and this file's own
   documented backlog, which still lists the cost-cap safeguard as an
   open, unstarted task. No such safeguard exists. Reverted to `False`
   -- almost certainly an accidental toggle, not a considered change.

   **Superseded same night.** That revert was itself wrong, for the
   exact reason this whole audit started: asserted "no safeguard exists"
   without re-checking the call site. `strategy.simulate_month` (the
   only real caller of `adjust_holding_window`) already restricts BOTH
   the band scan and the classifier to `_held` (this month's basket plus
   carried-in positions, ~10 symbols) -- a fix dated 03-Aug-2026, three
   weeks before this audit. `cycle_state.py`'s call site is inherently
   per-held-position already. Verified empirically, not just by reading
   code: scanned the full cached universe (194 symbols, ~653 sessions)
   for ratio breaches and found 23-26 in the entire window, not per day;
   both the NSE filing fetch and the LLM response are disk-cached by
   symbol+date, so a breach classified once is never re-fetched for the
   rest of that holding cycle. The full-universe-every-day cost that
   actually caused the timeout was `adjust_holding_window` running with
   `symbols=None`, already fixed 03-Aug-2026, unrelated to this flag.
   **`CORP_ACTION_GREY_ZONE_ENABLED` set back to `True`.** Full test
   suite green after the flip.
10. **Deleted `ACTUAL_FILLS_FILE`** (config.py) -- documented, zero
    readers anywhere in the repo, a mechanism someone designed for
    exactly this same "real fill differs from reconstruction" problem
    and never wired up. Superseded properly by book.json now actually
    being read (items 1-3), not by a second file.
11. **`split_adjust`'s `V4_SPLIT_RATIO_LOW/HIGH` was 0.6/1.8**, wider
    than `adjust_holding_window`'s already-agreed 0.72/1.40 standard
    (see `cycle_state.py`'s own comments: a 3:2 bonus, ratio 0.667, or a
    5:4 bonus, ratio 0.80, fell straight through the wider band
    undetected). This was not a fresh decision to make -- it was already
    made, just not applied to selection scoring. Fixed to 0.72/1.40 to
    match. Checked the cost side of this too: tightening the band adds
    only 3 incremental breach events across the full 194-symbol universe
    over ~653 cached sessions (23 -> 26 total), not a per-day cost.
    `market_breadth` (item 8) inherits the corrected threshold for free,
    since it reads the same config constant.

**Explicitly NOT changed, flagged instead of silently touched:**
`adjust_holding_window`'s hard band (0.72/1.40) plus its narrower
grey-zone band (0.85/1.18) is the deliberately-agreed standard;
selection now matches it (item 11). What's still unreconciled is
*mechanism*, not threshold: selection (`split_adjust`) has no grey-zone
concept at all -- inside its single hard band it always treats the move
as genuine, never consults the classifier. A moderate bonus (e.g. 0.80)
sitting inside [0.72, 1.40] is invisible to selection scoring even
though the identical ratio would trigger classifier review during
holding. Unifying that would change which stocks get selected some
months -- a real behavioural change, not a bugfix. Left as a flagged
follow-up (see `Pending Tasks`).

**Also flagged, not yet fixed (lower priority / needs a decision, not
a quick patch):** `ledger.performance()` silently drops any expiry with
no recorded ledger entry from both the sum and the CAGR denominator
(a failed run vanishes rather than showing as missing); `resolve_expiry`
falls back to the unadjusted last-Tuesday with only a warning if the
NSE holiday feed fails, which `cmd_sheet`'s guard could read as "not
expiry" and silently skip the whole month; `CASH_ACCRUES_RISK_FREE`/
`RISK_FREE_ANNUAL_PCT` are documented but have zero readers (empty
slots currently earn 0% for the month, not the intended pro-rated
risk-free rate); a few stray Unicode em-dashes in bare `print()` calls
in run_strategy.py that could raise `UnicodeEncodeError` on a non-UTF8
Windows console before any alert fires; `ledger.py` has a duplicate
`_append`-equivalent block instead of calling the existing helper.

Full test suite (59 tests) green after every change above; tonight's
real sheet re-verified byte-identical (aside from the intended fixes)
against an isolated copy after the full batch.

**Process note, repeated because it bit twice more tonight despite being
already documented above**: `--no-send` only blocks Telegram delivery.
It does NOT gate `ledger.record_note`/`entry_tracking.record` (bit once
tonight, 3 stray ledger lines, cleaned up) NOR `book.close_position`/
`book.adjust_shares` (bit again, prematurely applied tonight's not-yet-
placed TOP-UP amounts to the real book.json, caught and reverted before
send). Every one of `book.BOOK_FILE`, `entry_tracking.STATE_FILE`,
`cycle_state.STATE_FILE`, `config.LEDGER_FILE`/`LEDGER_ARCHIVE_DIR` must
be isolated before any real-data test run of `cmd_sheet` -- all four,
every time, no exceptions.

## Session 26-27-Aug-2026 — frozen-quote incident, ledger decommissioned as a live source, reconciliation check built

### Incident: KPITTECH/NATIONALUM marked FILLED against a quote a same-day fix had already superseded

`daily_report._solve_shares_to_slot`'s tie-break logic was fixed (prefer the lower-price candidate on a deviation tie — see its docstring) *after* the real 25-Aug expiry-evening `open_window()` call had already run and frozen the pre-fix quote (601.47/400.98) into `entry_tracking.json` for KPITTECH/NATIONALUM. The Day-1 fill check ran against that stale, too-generous quote, marked both FILLED, and `book.json` carried two phantom positions with zero real backing — the actual quotes investors were given (~579.79/391.32, communicated ahead of the automated message) never touched the day's low, so neither ever actually filled. Root cause is a general shape, not specific to this bug: `open_window()`/`advance()` compute a quote once and freeze it for the life of a stage; nothing previously re-checked a frozen value against current code.

Fixed live (backups taken first): both reverted to `status: pending` at the real quote in `book.json` and `entry_tracking.json`; the phantom `book.json` rows removed, then correctly restored as `seed_pending`-style blank placeholders (the canonical shape, not a full delete) via an unrelated audit-script side effect.

### `entry_tracking.reconcile()` / `apply_reconcile_fixes()` — the actual guard against a repeat

`entry_tracking.py` now exposes `reconcile(state=None, path=None)`: recomputes today's quote fresh, per pending symbol, using the exact same functions `open_window()`/`advance()` call (`_compute_stock_entry_band`/`_solve_shares_to_slot` for a Day-1 quote; the new shared `_vol_requote(close, sigma)` — factored out of `advance()`'s two inline Day-2/Day-3 formulas so there is one implementation, not two that could drift — for a Day-2/3 re-quote), and diffs it against the frozen `quote_price`. Returns a list of mismatches; empty means clean. `apply_reconcile_fixes(mismatches, state, path)` writes `current_code_quote` back programmatically (recomputing `shares`/`sl_price`/`exit_price` from it) with no manual transcription step between "the check found X" and "the file has X" — built after the first hand-applied fix (typing the corrected quote from `reconcile()`'s printed output into a new script) was itself flagged as the same transcription-risk class this check exists to eliminate.

First real-world run of `reconcile()` against live data immediately caught a second, genuine bug: the manual fix above corrected KPITTECH/NATIONALUM's status but left `quote_price` frozen at the Day-1 number instead of the Day-2 re-quote (594.43/403.11) that should have replaced it the moment the Day-1 miss was recorded (`sessions` already showed Day-1 as applied). Fixed via `apply_reconcile_fixes()`; also found and corrected a second stale leftover — `advance()`'s own internal `history` field (distinct from the `fill_history` written to `book.json`) still carried the old, wrong Day-1 "filled" record, which would have fed into the next real `advance()` call unless corrected. Verified clean via a full re-run of `reconcile()` (zero mismatches) and a fresh `render()`.

### Dormant per-symbol target/stop landmine closed

`book.open_position()` now accepts and stores `target_pct`/`stop_pct` (all four `entry_tracking.py` fill call sites — Day1, Day2, Day3, market-buy/REBASE — pass them through). `cycle_state.py`'s PENDING→HOLD transition reads them from the book row when present, falling back to the window's flat rate for older rows. Previously `cycle_state.py` always re-derived stop/target from its own flat, window-level percentages — harmless only because `LLM_TARGET_ENABLED = False` today, so every symbol shares one target and the two numbers coincide. The moment that flag is turned on (the per-symbol-target code path already exists, dormant, in `daily_report.py`/`llm_judgment.py`), the flat re-derivation would have silently diverged from what the investor was actually quoted. Verified against three scratch cases: a genuine per-symbol-target scenario, a legacy row with no stored percentages, and the no-book-record fallback.

### `ledger.jsonl` demoted to audit-only; `book.performance()` built

`ledger.performance()`/`monthly_summary()` fed the live "Performance to date" investor message (`run_strategy.py`'s `cmd_perf`/`cmd_sheet`) from a single `mtd_return_pct` frozen into `ledger.jsonl` once per day and never revisited — exactly why the REBASE correction to `book_archive.jsonl` (see the 25-Aug session below) never showed up in what investors were told: nothing ever re-derived the number after the correction. Per explicit instruction, `ledger.jsonl` is audit-only from here forward (a permanent record of what was said, for checking later whether the notes were right — `record_note()` still writes it) — nothing in the live pipeline reads it for a current number any more.

New `book.performance(path=None)` computes the same two cumulative conventions (`absolute_sum` additive, `absolute_comp`/`cagr` compounded) live, every call, from `book_archive.jsonl`'s `kind=="actual"` records plus `book.json`'s still-open positions marked to market via `cycle_state.json`'s live `last_close` cursor (legitimate, non-duplicated data — today's price, not a copy of entry/stop/target; `cycle_state.json` only ever holds the current cycle, so this only applies there). Same output shape as the old function, so `daily_report.render_performance()` needed no changes. `run_strategy.py`'s two live call sites (`cmd_perf`, `cmd_sheet`) switched over. Verified the formula against `cycle_state.to_report()`'s exact convention (`sum(pnl_pct) / config.PORTFOLIO_SIZE`) with non-trivial numbers, and verified the actual fix end-to-end: corrected a test archive record, called `performance()` again with zero other action, and the number updated immediately. Real effect on live data: the Aug (2026-07-28 expiry) figure moved from the stale 4.29% to the correct 5.44%, automatically, with no ledger write.

### Two structural things considered and explicitly rejected the same night

A full merge of `entry_tracking.json`'s temporal (not-yet-filled) state into `book.json` was attempted, then reverted. It solved a problem that never existed — a pending order has no row in `book.json` at all, so there was nothing there to go stale relative to a second copy — and introduced two real bugs while being built (an `entry_price`/`entry_date` leak where a stale in-memory copy overwrote a fresh `book.open_position()` write moments later; a clobber-guard needed for continuing-HOLD `shares`/`risk_anchor` to avoid overwriting a real position's live basis with next month's unexecuted plan a day early). `entry_tracking.json` stays a separate file, by design: it is genuinely a different concern (multi-day limit-chase state for something that isn't a position yet) from `book.json` (confirmed positions only), not an accidental duplicate.

Also decided: `cycle_state.json`'s full-overwrite-with-no-history behavior on every `open_cycle()` call does not need fixing — explicitly not required to be auditable; `ledger.jsonl`/`notes/*.txt` already cover the "what was said" audit need, and `cycle_state.json`'s own job (avoid re-ranking 208 symbols over 260 days every evening) is legitimate and unrelated.

### Dead data files removed

`data/backtest_result.json`, `backtest_result_v2.json`, `v4_backtest.json`, `latest.json` (referenced only by `legacy/*.py`), `carry_forward_v5.json` (a standalone research script, not the live pipeline), `fill_realism_v6_3stage.jsonl` (the backtest script's own scratch output, superseded by `book_archive.jsonl`) — confirmed unreferenced by anything live, deleted (backed up to `/tmp/dead_data_backup/` first).

### `entry_tracking.json` now self-clears once its window resolves

`mark_final_sent()` deletes the state file instead of just flagging `final_sent=True` and leaving it in place. Nothing unique is lost — every fact worth keeping already transferred to `book.json` the moment each slot filled, and `ledger.jsonl`/`notes/*.txt` already hold the permanent record of what was sent — and a resolved-but-still-present file was pure downside: it could be misread as live state by anything that doesn't specifically check `final_sent` first, right up until the next expiry's `open_window()` silently overwrote it anyway. Verified: `load()`/`is_window_active()` already handle a missing file exactly like "no window yet."

### Message formatting simplified

`render()`'s FILLED/LIMIT BUY/MANDATORY sections were two lines per stock (Entry/Qty, then SL/Exit) with no blank line between stocks — read as a wall of text on Telegram. Changed to one line per stock (`SYMBOL: N qty @ price  SL: X  Exit: Y`), explicit request.

### KPITTECH 54→55 shares question, and NSE tick-size rounding — both closed, no code change

Two follow-up questions, neither required a fix:

Why did KPITTECH's share count go from 54 (Day-1 limit, missed) to 55 (Day-2 re-quote) when the price also went UP (579.79 → 594.43)? Checked against the actual numbers: 54 × 579.79 = Rs 31,309 (-3.6% off the Rs 32,479 slot target this basket sizes every name to); 55 × 594.43 = Rs 32,694 (+0.66% off the same target). The allocation moved CLOSER to equal-weight, not further — well inside `ENTRY_MAX_WEIGHT_DEV_PCT` (10%). Mechanically the two quotes aren't even produced by the same formula: Day-1's price/share pair is solved jointly against the volatility band (`_solve_shares_to_slot` searches n-1/n/n+1); Day-2's price comes first, independently, off Day-1's realized volatility (`_vol_requote`), and shares are only derived after as `round(slot_target / price)`. The 54→55 jump is whole-share rounding around a fixed rupee target, not the strategy rewarding a price rise with a bigger position.

Separately: quoted prices (594.43, 32,479/n, etc.) aren't valid NSE tick multiples — real limit/SL orders must land on the exchange's price-band tick size (₹0.01 below ₹250, ₹0.05 up to ₹1,000, ₹0.10 up to ₹5,000, ₹0.50 up to ₹10,000, ₹1.00 up to ₹20,000, ₹5.00 above — last revised 15-Apr-2025). A `_round_to_tick()` helper was designed (price-band lookup, `round(round(price/tick)*tick, 2)`) but explicitly NOT wired in — decided to leave tick-rounding to the investor when they place the order, since the deviation is at most a few paise and not worth adding another rounding layer to live sizing code. No code changed as a result of this discussion.

### `risk_anchor`/`sigma1` left `None` by an earlier hand-fix, crashed the real 27-Aug production run

Tonight's 19:30 scheduled `cmd_daily` run crashed mid-way through Day-2 processing: `anchor_stop = anchor * (1 - stop_pct / 100.0)` in `advance()`'s `n == 2` branch, `TypeError: unsupported operand type(s) for *: 'NoneType' and 'float'`. `d["risk_anchor"]` was `None` for KPITTECH and NATIONALUM — residue from the 26-Aug incident fix a few nights earlier, where the phantom-FILLED revert restored `quote_price`/`shares`/`sl_price`/`exit_price` (the fields the pricing formula computes) but never touched `risk_anchor`/`sigma1` (facts about what Day-1 actually did, not part of the quote formula `reconcile()`/`apply_reconcile_fixes()` cover). Fixed by recomputing both from the real, already-cached Day-1 (26-Aug) bhavcopy — `_parkinson_sigma()` on the real H/L reproduced the exact 594.43/403.11 quotes already on file, confirming the derivation, not a guess. Also flagged and fixed a related silent-failure gap: the run's failure alert had nowhere to go because `TELEGRAM_ADMIN_CHAT_ID` was never set — added to `.env`.

This is also the incident that produced the standing rule at the top of this file (`## Working Rule — no hand-fixes to data files`): two separate fields silently missed by one hand-edit, same root cause both times.

### `--no-send` deleted the entry-tracking window before ever checking whether anything was sent

Running `run_strategy.py daily --no-send` to preview tonight's message did far more than preview: `mark_final_sent()` ran unconditionally inside `cmd_daily`'s `try` block, *before* the `--no-send` check even executed, deleting `entry_tracking.json` and writing a ledger record as if the "Final fill list" message had gone out — although `--no-send` means it explicitly hadn't. The very next real run (no flag) found `is_window_active()` false, fell straight through to the normal Stream-2 daily note, and delivered *that* instead — silently skipping the actual investor-facing message that reports today's Day-2 fills (KALYANKJIL/KPITTECH/NATIONALUM/NAM-INDIA: entry price, qty, SL, exit). The regular note that did go out was numerically correct (fills are written to `book.json` inside `advance()` regardless of `--no-send`, which is intentional — fills are market fact, not something to gate on message delivery) but never told investors those four names had just filled or at what levels.

Fixed by moving only `mark_final_sent()` to run after `alerts.send()` confirms success (`entry_tracking.record()` deliberately left where it was — `cmd_sheet` documents the same "record before sending, the decision is what matters" pattern elsewhere in this file, and gating record() too would have contradicted that existing, intentional design rather than fixed a bug). `mark_final_sent()`'s own docstring already says it should run "right after the final message has already been rendered and recorded" — the code just didn't honor its own documented order. Side effect: this also closes a second, previously-unnoticed bug where a genuine Telegram outage on a resolved window destroyed the state file before knowing whether delivery had actually failed, with no way to retry. The missed message itself was recovered by resending the literal text the `--no-send` run had already printed (a one-off script, deleted after use) — not recomputed or retyped.

### `render()` numbering restored

`render()`'s FILLED/LIMIT BUY/MANDATORY sections lost their numbering in the 26-Aug two-line-to-one-line rewrite (`render_new_investor_day0` still numbers its list; this one didn't). Restored, numbered separately within each section (1, 2, 3... per section, not one running count across all three) since each is a distinct checklist an investor works through.

## Pending Tasks

- **New, separate strategy: push-based long-term strategy identification.**
  (Idea captured 17-Aug-2026, not scoped or started.) Distinct from the
  monthly momentum engine above -- a system that continuously scans the
  market and news for longer-horizon setups and pushes alerts, rather
  than waiting for a monthly expiry-driven rebalance. Grew out of asking
  whether the 14-Aug-2026 SAIL/metals sector flush was predictable from
  news in advance rather than explainable after the fact -- conclusion
  was that reactive financial journalism doesn't give an edge, but
  leading indicators (commodity prices, scheduled macro events, PSU
  stake-sale-type chatter) might, IF systematised. This system currently
  ingests no news/sentiment feed at all (the only LLM call is narrow
  corporate-action-filing classification) -- a real news-driven layer
  would be new infrastructure, not an extension of `strategy.py`. Needs
  its own scoping pass before any build starts: what "long-term" means
  here, what counts as a push trigger, what data sources, and how it
  avoids the standard news-sentiment-signal trap (looks good in
  backtest, evaporates out of sample).

- **Re-run every 03-Aug comparison through `simulate_month`.** See
  `research/README.md` for the list.
- **Add a dead-man's alarm on alert delivery** -- if no message lands on a
  trading day, escalate somewhere that is not the same channel.
- **Split-guard `from52wh`** -- BAJFINANCE read -87.9% below its 52w high
  on 31-Jul-2026; that was a bonus, not a drawdown. `PICKING_METHOD.md`
  depends on this feature.
- **Give `split_adjust` (selection scoring) a grey-zone concept**, so a
  moderate bonus (e.g. ratio 0.80, inside the hard band) gets classifier
  review the same way it already does during holding P&L, instead of
  being silently treated as a genuine price move. Selection has no
  per-symbol restriction to lean on (it scores the whole universe by
  design), so this needs its own cost thought, not a copy-paste of the
  `_held` restriction.
- ~~No fresh-start additive runner~~ DONE 03-Aug-2026: `research/run13.py`.
- ~~Add a cost cap so grey-zone corporate-action classification can be
  turned on~~ DONE 25-Aug-2026: the cap already existed
  (`simulate_month` restricts `adjust_holding_window` to `_held`,
  ~10 symbols, since 03-Aug-2026) -- verified empirically and turned
  `CORP_ACTION_GREY_ZONE_ENABLED` back on.
- ~~Revert the losing config~~ DONE 02-Aug-2026.
- ~~Regime-pegged stop~~ DONE 02-Aug-2026 (breadth, 45% threshold).
- ~~Validate the regime stop on the other 8 cycles~~ DONE. All 13 run
  through the real engine: +39.34% accrued, worst month -3.98%, t 1.58.
  Mar-2025 is the strongest out-of-sample point: breadth 27.3% called the
  wide stop and returned +6.01% against -3.16% for the tight stop.
- **Re-run the four rejected hypotheses through the real engine.** The
  LLM targets, Gaussian volatility centring, price-momentum selection and
  sector-first were all rejected using the flawed `sim()` helper. The
  rankings are probably right but none was measured against the true
  baseline.
- ~~Test the ATR conviction SOP~~ REJECTED 03-Aug-2026, -15.36pp.
- ~~Test the v1.1 selection rule~~ REJECTED 03-Aug-2026, -24.93pp.
- **Test limit-order entry.** Place buys ~2% below the expiry close
  instead of at the next open, and measure fill rate and return over the
  five cycles. This is the one mechanism with direct evidence behind it
  (27/29 of the comparison portfolio's entries were below the next open)
  and it has NOT been tested on our basket.
- **Rotate the Gemini API key** -- pasted into a chat transcript
  01-Aug-2026, still live.
- **Model the +/-10% price band in the backtest.** Targets currently fill
  on any day whose high touches them, which is impossible below +27.3%.

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
- **Model the ±10% price band in the backtest.** Targets currently fill
  on any day whose high touches them, which is impossible below +27.3%.
  Until this is done the +31.77% is overstated by an unknown amount.
- **Windows Task Scheduler entries not yet created** — the system does
  not run unattended until they are.
- **Rotate the Gemini API key.** It was pasted into a chat transcript on
  01-Aug-2026.
- Delete the stale "sandbox blocks nseindia.com" docstring in
  `nse_client.py` — disproven.
- **V5 change #3**: unspecified by user as of 01-Aug-2026 — get this
  before finalizing V5's `strategy.py`/`config.py`.
- ~~Commit the uncommitted work~~ — DONE 01-Aug-2026, plus a private
  GitHub remote at `ranjankai/momentummori` (public; see below).
- ~~Extend history~~ — PARTIAL: `data/cache` now runs from Jan-2024
  (636 days) rather than Jan-2025. Still far short of 2018.
- (superseded) **Commit the uncommitted work.** As of 01-Aug-2026, `config.py`,
  `pipeline.py`, `run_backtest.py`, `scoring.py`, both test files, and
  `CONTEXT.md` are modified but uncommitted; `strategy.py`,
  `run_strategy.py`, `fo_mktlots.csv`, `config/sectors.csv`, and several
  `fetch_*_fo.py` scripts are untracked. This includes all of V4, V5's
  carry-forward change, and the sector-cap/volatility signal work —
  none of it is in git history yet. Needs a review pass and a commit
  (or several atomic ones) before it's safe to treat as a checkpoint.
