# Monthly Stock Picking Methodology (judgement-assisted)

**For a fresh Claude instance. Do not open `top 10 scrips monthly.xlsx`, any
Altcase report, or `/tmp/alt*.json` before producing your picks. Those hold
the answer key. Produce the ten names first, save them, then score.**

Universe: NSE F&O. Output: exactly 10 symbols, ranked 1-10.

---

## 0. Setup

Repo: `C:\Users\ranjan\Documents\momentum-tracker` (bash:
`/sessions/<session>/mnt/momentum-tracker`). Price cache covers Oct-2023
to Jul-2026 in `data/cache/`.

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

---

## 2. Read the regime FIRST — this decides everything downstream

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
**8 of 206** names were above both DMAs and universe median 20D was
−12.58%. Any rule demanding "uptrend" returns an empty or absurd list
there. In Jul-2026, 88 of 206 qualified and momentum worked normally.

---

## 3. Hard filters (applied before any ranking)

Always:

1. `turnover` ≥ ₹5 crore/day (20-session mean)
2. Not in the F&O ban list for the selection date
   (`https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_DDMMYYYY.csv`)
3. At least 210 sessions of history

Regime-dependent:

- **TRENDING** — require `close > dma20` AND `close > dma50`
- **MIXED** — require `close > dma20` only
- **DAMAGED** — no DMA filter at all; it would empty the list

---

## 4. Rank

Score the survivors. Weights are guidance, not gospel — the point of a
judgement layer is that you may depart from them with a stated reason.

**TRENDING**

```
0.35 z(r20) + 0.20 z(r60) + 0.20 z(from52wh) + 0.15 z(sector_rank) + 0.10 z(volsurge)
```

`sector_rank`: rank sectors by median `r20` across the universe, leaders
first, and score member stocks by their sector's rank. Leading sectors
have historically carried the basket.

`from52wh` enters POSITIVELY — closer to the high is better. In Jul-2026
a basket of names within ~1.5% of their highs was the right answer.

**DAMAGED**

Invert the strength terms. Rank on:

```
0.40 z(−from52wh) + 0.30 z(r20 relative to universe median) + 0.20 z(sector_rank) + 0.10 z(volsurge)
```

i.e. prefer the most beaten names that are still falling LESS than the
market. In Apr-2026 the right basket had median `r20` of −7.91% against
a universe median of −12.58% — outperformers inside a decline, sitting
~22% below their highs.

**MIXED** — blend. Drop the `from52wh` term entirely and lean on `r20`
relative strength plus sector leadership.

---

## 5. Portfolio construction

Walk the ranked list top-down:

- max **3 per sector**
- stop at **10 names**
- if fewer than 10 survive the filters, take what there is and hold the
  remainder in cash; do not relax the liquidity floor to fill slots

---

## 6. Judgement overlay — where you are expected to deviate

Apply these as explicit, stated overrides:

1. **Reject bounce-in-a-broken-chart.** A name more than 40% below its
   52-week high with `hv20` above ~50 is a dead-cat candidate, not a
   leader. Exclude in TRENDING and MIXED regimes even if `r20` ranks it
   highly. (KAYNES, Aug-2026: `r20` +14.1% but 50.6% off its high with
   `hv20` 59.9.)
2. **Prefer the intact chart when two names score within ~10%.** Take the
   one nearer its 52-week high.
3. **Do not take three names from one sector unless that sector is a
   clear leader** — top 3 by sector median `r20`.
4. **`from52wh` is only trustworthy if you ran the adjustment in step 1.**
   With it, BAJFINANCE reads −0.9% instead of −87.9%. Without it, every
   name that has split or issued a bonus in the last year looks crashed.
   If a reading is worse than about −60%, check you did step 1 before
   acting on it.
5. **Ignore derivatives data entirely** — rollover, cost of carry, open
   interest. That is what the production V4 engine ranks on, and it
   selects a near-disjoint basket (historical overlap with the trend
   approach ~7%). This methodology is deliberately price-and-volume only.

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
slots left in cash: N
```

Save to `data/picks_<YYYY-MM>.json` BEFORE looking at any comparison
data.

---

## 8. Months to run

Feb, Mar, Apr, Jul 2026 (May-2026 is absent from the comparison set and
Jun-2026 is empty). Run each independently — do not look at a later
month's outcome when picking an earlier one.

---

## What the target actually looks like — measured, not guessed

The comparison portfolio's own picks, profiled on the selection date.
This is the single most useful thing in this document: it tells you what
you are aiming at before you start, and it is measured from their
published baskets, not inferred.

| month | their 10: above 20+50 DMA | median 20D ret | universe median 20D | median from 52w high |
|---|---|---|---|---|
| Jul-26 | 10/10 | +13.53% | +1.74% | −1.4% |
| Feb-26 | 7/10 | +6.54% | −3.68% | −5.5% |
| Mar-26 | 4/10 | −0.62% | +2.11% | −20.0% |
| Apr-26 | 2/10 | −7.91% | −12.58% | −22.5% |

Read it carefully, because it is the whole problem:

- **Jul-26** they bought leaders sitting AT their highs. A plain trend
  ranker reproduces **6 of their 10** there. That is the benchmark to beat.
- **Apr-26** they bought wreckage — eight of ten BELOW both DMAs, 22%
  off their highs — but names falling LESS than a universe down 12.58%.
  Relative strength inside a decline, not absolute strength.
- **Feb and Mar contradict any simple market-state rule.** Feb had a WEAK
  tape (−3.68%) and they bought STRENGTH (+6.54%). Mar had a RISING tape
  (+2.11%) and they bought WEAKNESS (−0.62%, 20% off highs). Whatever
  switches their mode, it is not the market's direction that month.

A mechanical grid search over 486 weight combinations scored **11/40**
across these four months, and **0/10 in Mar-26 under every single
combination**, because the DMA gate deletes six of the ten target names
before scoring begins. That is why sections 2 and 6 exist. Beating 11/40
is the bar; the regime split and the judgement overlay are the proposed
means, and both are untested.

## Known limits, stated honestly

- The comparison portfolio screens **NIFTY 500**, not F&O, and about 16
  of its 38 WEEKLY shortlisted names are cash-only. Its MONTHLY baskets,
  though, are reachable: all 10 names mapped to the F&O universe in every
  one of Feb, Mar, Apr and Jul 2026. So 10/10 is attainable in principle
  for these months -- do not treat any miss as structural.
- The 11/40 grid result above is the FITTED ceiling — weights chosen after
  seeing the answers, on four months. Live performance would be lower.
  Six weights on four observations is memorisation, which is exactly why
  a judgement layer is being tried instead of more weight-tuning.
- The regime rule itself is not established. Feb-2026 had a weak tape and
  the target basket was strong; Mar-2026 had a rising tape and the target
  basket was weak. A simple market-state switch does not reconcile them.
  Treat section 2 as a hypothesis you are testing, not a known truth.
