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
uni = strategy.load_fo_universe()
sec = strategy.load_sector_map()
hist = strategy.load_price_history(<anchor date mid-month>, uni, days=300)
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
4. **Sanity-check `from52wh` against corporate actions.** NSE bhavcopy is
   unadjusted. A reading worse than about −60% is usually an unadjusted
   split or bonus, not a drawdown. Verify before excluding a name on it.
   (BAJFINANCE showed −87.9% in Aug-2026; it was a bonus.)
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

## Known limits, stated honestly

- The comparison portfolio screens **NIFTY 500**, not F&O, and roughly
  16 of its 38 weekly shortlisted names are cash-only. Some of its picks
  are structurally unreachable from an F&O universe.
- A pure mechanical version of this method was grid-searched over 486
  weight combinations on 4 months. Best fitted result was 11/40 (~2.75/10),
  and Mar-2026 scored 0/10 under every combination because the DMA gate
  deleted 6 of the 10 target names. That failure is what motivated the
  regime split in section 2 and the judgement overlay in section 6 — both
  are UNTESTED.
- The regime rule itself is not established. Feb-2026 had a weak tape and
  the target basket was strong; Mar-2026 had a rising tape and the target
  basket was weak. A simple market-state switch does not reconcile them.
  Treat section 2 as a hypothesis you are testing, not a known truth.
