# Monthly Stock Picking Methodology

Universe: NSE F&O. Output: **exactly 10 symbols**, ranked 1–10.

Price, volume AND derivatives data are all available to you. The
production V4 engine ranks on derivatives alone — 0.50 z(volatility) +
0.30 z(rollover) + 0.20 z(cost of carry) — and this method is built
around price structure instead, because a ranking that ignores where a
stock sits in its own trend has a blind spot.

That is a change of emphasis, not a ban. Use the derivative signals as
confirmation, as a tie-break, or to reject a name whose chart looks fine
but whose positioning does not. Say when you did.

Work each month independently. Do not look at a later month's outcome
when picking an earlier one, and do not look at any month's realised
returns before saving your picks.

---

## 0. Setup and where the data lives

Repo root: `C:\Users\ranjan\Documents\momentum-tracker`
(in the Linux sandbox: `/sessions/<session>/mnt/momentum-tracker`)

| what | path |
|---|---|
| cash bhavcopy cache, one CSV per session | `C:\Users\ranjan\Documents\momentum-tracker\data\cache\cm_YYYYMMDD.csv` |
| F&O bhavcopy cache, one CSV per session | `C:\Users\ranjan\Documents\momentum-tracker\data\cache\fo_YYYYMMDD.csv` |
| "NSE published nothing" markers | `...\data\cache\cm_YYYYMMDD.nodata` |
| F&O universe (eligibility + lot sizes) | `C:\Users\ranjan\Documents\momentum-tracker\fo_mktlots.csv` |
| sector map, symbol → sector | `C:\Users\ranjan\Documents\momentum-tracker\config\sectors.csv` |
| cached derivative signals, per expiry | `C:\Users\ranjan\Documents\momentum-tracker\data\signals_cache\sig_YYYYMMDD.csv` |
| corporate-action filings cache | `C:\Users\ranjan\Documents\momentum-tracker\data\corp_cache\` |
| your output | `C:\Users\ranjan\Documents\momentum-tracker\data\picks_YYYY-MM.json` |
| run log | `C:\Users\ranjan\Documents\momentum-tracker\logs\app.log` |

The cash cache runs from **Oct-2023 to Jul-2026**; the F&O cache holds
expiry dates only. Do not hand-parse these — the loaders below normalise
NSE's column names, drop duplicate series rows and handle missing days.

**Selection date** = the last trading session strictly BEFORE the 1st of
the target month. Not the F&O expiry. (For Jul-2026 that is 30-Jun-2026.)

Everything below uses ONLY data available at that session's close.

---

## 1. Build the feature table

```python
import strategy, pandas as pd, numpy as np
uni  = strategy.load_fo_universe()
sec  = strategy.load_sector_map()
hist = strategy.load_price_history(<anchor date mid-month>, uni, days=300)

# MANDATORY. NSE ships unadjusted prices: after BAJFINANCE's 10:1 on
# 16-06-2025 the old high still sits in the file, so `from52wh` read
# -87.9% when the stock was 0.9% off its high. back_adjust=True restates
# history and leaves today's price real.
hist = strategy.adjust_holding_window(hist, sorted(hist), back_adjust=True)
```

Per symbol, from the last 260 sessions up to the selection date:

| feature | definition |
|---|---|
| `r5`,`r20`,`r60` | close / close N sessions ago − 1 |
| `dma20/50/100/200` | trailing means of close |
| `stack` | `(c>dma20) + (dma20>dma50) + (dma50>dma100) + (dma100>dma200)`, 0–4 |
| `from52wh` | close / max(high over 252 sessions) − 1 |
| `hv20` | stdev of daily returns over 20 sessions × √252 |
| `atr20` | mean true range over 20 sessions |
| `volsurge` | mean turnover last 5 sessions / mean last 20 |
| `turnover` | mean turnover last 20 sessions |
| `sector` | from `strategy.load_sector_map()` |

Discard symbols with fewer than 210 usable sessions.

### Derivative features, for the same date

Available but NOT part of the core ranking. Use them as confirmation or
a tie-break, and say when you did.

```python
import nse_client, scoring
# the last monthly expiry ON OR BEFORE your selection date
ex  = strategy.expiry_for(<year>, <month>, trading_days=strategy.known_trading_days())
fo  = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
sig = strategy.compute_signals_cached(hist, fo, ex, uni)
#   sig.volatility     63d annualised SD of daily returns
#   sig.rollover       % of OI carried into the next series
#   sig.cost_of_carry  annualised futures premium
#   sig.turnover_cr    median daily turnover, INR crore
```

Richer per-symbol derivatives — basis, open interest across three
series, futures volume, rollover against a 3-month baseline — are in
`tools_features.fo_snapshot(expiry)` in the repo root.

Two things worth knowing before you lean on them:

- **Rollover is frozen between expiries.** It only changes when a series
  rolls, so on any non-expiry selection date it is a stale reading of
  what positioning looked like at the last expiry, not today.
- **High volatility scores HIGHER in the production engine**, not lower.
  That is deliberate and was measured. If you use volatility, be explicit
  about which direction you are treating as good and why.

---

## 2. Read the regime FIRST — it decides everything downstream

Compute across the eligible universe:

- `breadth` = % of names above their own 200 DMA
- `med_r20` = universe median 20-day return
- `med_r60` = universe median 60-day return
- `pct_above_20_50` = % of names above both their 20 and 50 DMA

Classify:

- **TRENDING** — `med_r20 > 0` and `pct_above_20_50 > 35%`
- **DAMAGED** — `med_r20 < −5%` or `pct_above_20_50 < 15%`
- **MIXED** — anything else

This matters more than any individual stock feature. In Apr-2026 only
**8 of 206** names were above both DMAs and the universe median 20-day
return was −12.58%. Any rule demanding "uptrend" returns an empty or
absurd list there. In Jul-2026, 88 of 206 qualified and ordinary
momentum ranking worked.

**In a DAMAGED month, absolute strength does not exist.** What exists is
relative strength: names falling less than the market. Rank on that.

---

## 3. Hard filters (before any ranking)

Always:

1. `turnover` ≥ ₹5 crore/day (20-session mean)
2. Not in the F&O ban list for the selection date
   (`https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_DDMMYYYY.csv`)
3. At least 210 sessions of history

Regime-dependent:

- **TRENDING** — require `close > dma20` AND `close > dma50`
- **MIXED** — require `close > dma20` only
- **DAMAGED** — no DMA filter; it would empty the list

---

## 4. Rank

Weights are guidance, not gospel. The point of a judgement layer is that
you may depart from them with a stated reason.

**TRENDING**

```
0.35 z(r20) + 0.20 z(r60) + 0.20 z(from52wh) + 0.15 z(sector_rank) + 0.10 z(volsurge)
```

`sector_rank`: rank sectors by median `r20` across the universe, leaders
first, and score member stocks by their sector's rank.

`from52wh` enters POSITIVELY — closer to the high is better.

**DAMAGED**

Invert the strength terms:

```
0.40 z(−from52wh) + 0.30 z(r20 relative to the universe median) + 0.20 z(sector_rank) + 0.10 z(volsurge)
```

Prefer beaten names that are still outperforming a falling market.

**MIXED** — drop the `from52wh` term entirely and lean on `r20` relative
strength plus sector leadership.

---

## 5. Portfolio construction — ALL TEN SLOTS MUST BE FILLED

Walk the ranked list top-down, max **3 per sector**, until you have 10.

**You must return exactly 10 names. Cash is not an option here.** If the
filters leave you short:

1. Relax the regime DMA filter first (it is the most likely culprit — in
   a bad month it can cut the pool to single digits). Never relax the
   liquidity floor or the ban-list check; those are safety, not selection.
2. If still short, relax the sector cap from 3 to 4, taking the
   highest-ranked names.
3. If still short, fill from the highest-ranked remaining names that pass
   the liquidity and ban filters, whatever their trend state.

State plainly which relaxations you used and for which slots. A 10-name
list with two stated compromises is the deliverable; an 8-name list is
not.

---

## 6. Judgement overlay — where you are expected to deviate

Apply these as explicit, stated overrides:

1. **Reject a bounce in a broken chart.** A name more than 40% below its
   52-week high with `hv20` above ~50 is a dead-cat candidate, not a
   leader. Exclude in TRENDING and MIXED even if `r20` ranks it highly.
   (KAYNES on 31-Jul-2026: `r20` +14.1% but 50.6% off its high, `hv20`
   59.9.) In a DAMAGED regime this rule does not apply — there,
   everything is below its high.
2. **Prefer the intact chart when two names score within ~10%.** Take the
   one nearer its 52-week high.
3. **Do not take three from one sector unless that sector is a clear
   leader** — top 3 by sector median `r20`.
4. **`from52wh` is only trustworthy if you ran the adjustment in step 1.**
   With it, BAJFINANCE reads −0.9% instead of −87.9%. If a reading is
   worse than about −60%, check step 1 before acting on it.
5. **Derivatives are a second opinion, not the ranking.** If a name
   ranks well on price but its cost of carry is deeply negative, or its
   rollover collapsed at the last expiry, that is a reason to prefer the
   next candidate — state it. Do not rebuild the production engine's
   score here; it already exists and picks a different basket.

---

## 7. Output format

```
regime: TRENDING | MIXED | DAMAGED
  breadth X%, med_r20 X%, med_r60 X%, pct_above_20_50 X%

 #  symbol  sector  r20  r60  from52wh  hv20  turnover(cr)  reason
 1  ...
...
10  ...

overrides applied: <list, with reasons>
filters relaxed to reach 10: <list, or "none">
```

Save to `data/picks_<YYYY-MM>.json`.

---

## 8. Months to run

**Feb, Mar, Apr and Jul 2026.** Each independently.

---

## Status of this method

Sections 2, 4 and 6 are a **hypothesis**, not a validated recipe. The
regime split in particular is proposed rather than proven — it was
written because a single fixed ranking demonstrably fails in months where
the whole market is below its moving averages, but the specific
thresholds and weights here have not been shown to work.

You are being asked to test it and to exercise judgement where it is
silent or wrong, not to execute it mechanically. If your reading of the
data contradicts a rule above, follow the data and say so.
