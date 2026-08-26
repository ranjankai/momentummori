# Backtest log — every experiment run against V4

One file, everything tried. Each entry says what the idea was, how it was
implemented, what it returned, and why it did or did not survive.

Compiled 04-Aug-2026. Data: NSE F&O universe, cash + F&O bhavcopy cached
from Oct-2023, 13–15 monthly cycles from Mar-2025 to Jul-2026.

---

# 0. READ THIS FIRST — why the same strategy shows several different totals

Five numbers for "V4 over the history" appear below. They are not
contradictory; they measure different things. Quoting the wrong one is the
easiest mistake to make here.

| number | convention | veto | produced by |
|---|---|---|---|
| **+39.57%** | fresh-start additive, 13 cycles | no | `research/run13.py` — **CANONICAL** |
| +36.87% | carry-forward chained, 13 cycles | no | retired `tools_run13.py` |
| +38.97% | fresh-start additive, 13 cycles | yes | `research/drawdown_filter.py` |
| +40.13% | fresh-start additive, 15 cycles | yes | `research/dod_threshold.py` |
| +34.02% | fresh-start additive, 13 cycles | no | pre-bugfix, superseded |

Four axes explain the differences, all legitimate:

- **Carry-forward vs fresh start.** Carry-forward does not sell a name that
  survives to expiry and stays in the new basket. Fresh start closes
  everything at the rollover open. Agreed reporting convention is **fresh
  start, additive** — monthly returns summed, never compounded, because
  investors enter and exit at will.
- **Surveillance veto on or off.** Live applies it. Backtests mostly
  cannot: NSE publishes ASM as a *current* snapshot with no per-date
  archive, so vetoing a 2025 basket with today's list is look-ahead.
- **13 vs 15 cycles.** Later work extended to Apr-2026 and Jun-2026.
- **Pre- or post- the 03-Aug-2026 bug fixes** (section 8).

**When comparing two arms of an experiment, only compare within the same
run.** Both arms always share a harness, so the *difference* is meaningful
even where the *level* is not comparable across sections.

---

# 1. The live configuration

```python
V4_WEIGHTS = {"volatility": 0.50, "rollover": 0.30, "cost_of_carry": 0.20}
REGIME_STOP_ENABLED       = True
REGIME_BREADTH_THRESHOLD  = 45.0     # % of universe above its own 200 DMA
REGIME_STOP_TIGHT_PCT     = 5.0      # breadth >= 45
REGIME_STOP_WIDE_PCT      = 10.0     # breadth <  45
V4_TARGET_PCT             = 40.0
V4_REDEPLOY_ENABLED       = False    # a freed slot holds cash to expiry
MAX_SECTOR_WEIGHT_PCT     = 30       # max 3 of 10 in one sector
MIN_TURNOVER_CRORE        = 5.0
VETO_ENABLED              = True     # ASM/GSM surveillance
PORTFOLIO_SIZE            = 10       # equal weight
```

## How it works

**Selection**, once per month on the F&O expiry close:

1. Universe = names with a live stock-futures contract (`fo_mktlots.csv`),
   about 206–208 symbols.
2. Drop anything under ₹5 crore median daily turnover. Thin and volatile is
   where a market-order stop fills worst.
3. Score each survivor:
   `0.50·z(volatility) + 0.30·z(rollover) + 0.20·z(cost of carry)`
   volatility = 63-day annualised SD of daily returns; rollover = % of open
   interest carried into the next series; cost of carry = annualised
   futures premium.
4. Sort descending, walk the list, take 10 with **max 3 per sector**.
5. Apply the ASM/GSM veto, backfilling from the ranking.

**Volatility is scored HIGHER-is-better.** This inverts the source deck and
was not a guess: across 13 months and 2,677 stock-months, realised
volatility was the only feature whose top decile held meaningfully more
than its share of winners (lift 1.5–2.0×). Rollover alone scored *below*
chance (lift 0.86).

**Execution:** buy all ten at the open of the first session after expiry,
equal weight. Stop and target are resting broker orders and can fill
intraday. No discretionary intraday trading.

**Exits:** stop 5% or 10% below entry, fixed on entry day; target +40%;
otherwise sell at the open after the next expiry. A freed slot holds cash —
no mid-month replacement.

## Canonical result — 13 cycles, fresh start, additive

| cycle | breadth | stop | return |
|---|---|---|---|
| 2025-03 | 27.3 | 10% | +4.97% |
| 2025-04 | 45.5 | 5% | +0.28% |
| 2025-05 | 54.0 | 5% | +1.50% |
| 2025-06 | 63.3 | 5% | −3.20% |
| 2025-07 | 56.8 | 5% | −4.25% |
| 2025-08 | 50.0 | 5% | +4.47% |
| 2025-09 | 55.4 | 5% | +4.29% |
| 2025-10 | 71.2 | 5% | +2.46% |
| 2025-11 | 59.0 | 5% | +3.38% |
| 2025-12 | 55.1 | 5% | −3.51% |
| 2026-01 | 40.0 | 10% | +13.61% |
| 2026-02 | 48.1 | 5% | −4.02% |
| 2026-03 | 19.4 | 10% | +19.61% |

**sum +39.57% · mean 3.04%/mo · sd 7.01 · worst −4.25% · positive 9/13 · t 1.57**

A t-statistic of 1.57 on 13 observations is **not significant**. Everything
here is a hypothesis under live test.

---

# 2. THE ONE THING THAT WORKED — the regime-pegged stop

**Idea.** Choose the stop width once, on expiry day, from market breadth —
the share of the F&O universe above its own 200-day average. Breadth ≥ 45%
is a healthy market, so cut losses fast at 5%. Below 45% the market is
already beaten down and you are buying a bounce, so give it 10% and avoid
being shaken out.

**Result: adopted.** It beats every fixed stop width *and* beats holding
with no stop at all:

| stop | sum | worst month | positive |
|---|---|---|---|
| fixed 3% | +15.38% | −3.20% | 7/13 |
| fixed 5% | +20.02% | −4.25% | 8/13 |
| fixed 7% | +26.82% | −4.95% | 8/13 |
| fixed 10% | +32.08% | −7.70% | 9/13 |
| fixed 15% | +29.33% | −9.08% | 9/13 |
| no stop at all | +35.76% | −9.02% | 9/13 |
| **regime 5/10** | **+39.57%** | **−4.25%** | **9/13** |

The wide stop fired in only 3 of 13 cycles — Mar-25, Jan-26, Mar-26 — and
was right in all three. The mechanism is not "wider stops earn more"; it is
**"a wider stop stops you being shaken out of a rebound"**. Wide-stop months
run 1–4 trades, tight-stop months 7–10.

**Caveat.** Five observations were used to pick the 45% threshold, after
seeing the outcomes. A coin-flip rule separates 5 points about 1 time in 10.

---

# 3. Exit-rule experiments — all rejected

## 3.1 ATR conviction SOP (trailing ratchet)

**Idea.** Replace flat stop/target with ATR-scaled levels by conviction
tier. Ranks 1–3 stop 2.5×ATR20, target 6×ATR20; ranks 4–7 2.0×/5×; ranks
8–10 1.5×/4×. ATR frozen on entry day. The target does **not** sell — it
promotes INITIAL → WINNER, after which a ratchet stop trails the highest
high and only moves up.

**Result: −15.36pp.**

| | sum | mean | sd | worst | positive |
|---|---|---|---|---|---|
| V4 exits | +34.02% | 2.62% | 7.47 | −6.18% | 8/13 |
| ATR SOP | +18.66% | 1.44% | 7.92 | −6.09% | 7/13 |

**Why it failed.** The promotion gate never opens. Median target distance
is **18.05%** in a ~34-day window, so only **21 of 130 positions** reached
WINNER. The trailing stop — the entire point — fired 17 times in 130. Five
of six positions ran and died on the initial stop, leaving the ratchet, the
tiers and the frozen ATR decorative.

Adding carry-forward made it **worse** (+16.99%): a carried position keeps
its original frozen entry, so after a bad month the target sits further
overhead and promotions fell from 21 to 3.

Conviction tiers showed no monotonic payoff — Top 3 mean −0.19%, Middle 4
**+4.07%**, Bottom 3 −0.45%. Rank 1–3 did no better than rank 8–10.

## 3.2 Portfolio "drop from entry" exit (EP rule)

**Idea.** Track the book versus its entry level; once it closes X% below
entry, sell everything.

**It looked strong.** On the four cycles held Feb–May 2026, once the
portfolio closed **2% below entry it never returned to breakeven** — 19
observations, zero recoveries.

**Result: rejected.** Those 19 observations are one month sampled 19 times
— all from Mar-2026, the only losing cycle in that window. Across 15
cycles no level survives:

| level | cycles breaching | later closed back above |
|---|---|---|
| −2.0% | 7 | 3 |
| −3.0% | 5 | 4 |
| −4.5% | 2 | 2 |
| −6.0% | 1 | 1 |

**2025-03 is the counterexample at every depth** — it fell **7.58% below
entry** and recovered to +2.23%:

```
04-03  +1.14    04-04  -3.12    04-07  -7.58    04-08  -6.74
04-11  -4.58    04-16  -1.16    04-21  +1.66    04-24  +2.23
```

## 3.3 Regime-conditional EP exit

**Idea.** Since 2025-03 was a wide-stop cycle, apply the −2% exit **only in
tight-stop cycles**.

**Result: −1.80pp. Rejected.**

| | sum |
|---|---|
| base | 40.13% |
| flat −2% everywhere | 35.65% |
| tight-stop cycles only | 38.33% |

It fires in 6 tight cycles and is wrong in three — Apr-25 (−1.05% →
−2.88%), Dec-25 (−2.36% → −3.36%), Jun-26 (−1.07% → −2.20%) — converting
shallow losers into deeper ones.

## 3.4 Per-stock EP analysis

**Finding**, 150 stock-cycles / 3,190 sessions:

| once a stock closed X% below entry | never returned to entry |
|---|---|
| ≤ −2% | 94.9% (1,170 of 1,233) |
| ≤ −5% | 99.9% (1,079 of 1,080) |
| ≤ −8% | 100% (59 of 59) |

An EPmax exists too: once below −5%, **76 of 79 stocks never closed above
−5% again**.

**Not actionable.** The 5% stop already sells these names in tight cycles.
The observations below −5% are overwhelmingly *wide-stop* cycles where the
room is deliberate. The statistic describes the room being given.

## 3.5 Close-based exit inside wide-stop cycles

**Idea.** Test 3.4 directly — in wide-stop cycles only, add a close-based
exit at −X% on top of the 10% intraday stop.

**Result: rejected at every level that fires.**

| | base | 3% | 4% | 5% | 6% | 8% |
|---|---|---|---|---|---|---|
| all cycles | 40.13% | 28.90% | 34.84% | 39.46% | 38.26% | 40.64% |
| wide only | 35.48% | 24.24% | 30.19% | 34.80% | 33.60% | 35.99% |

Mar-2026 — best cycle in the history at +22.90% — loses **7.31pp** at a 3%
close exit. The apparent +0.51pp at 8% is one name in one cycle; at that
width the 10% intraday stop usually sells first.

## 3.6 ETDP trailing stop (drop from entry-to-date peak)

**Idea.** Sell once a stock closes D% below its highest close since entry.

**Frequency first** — once D% off the peak, does it ever close above that
trigger again?

| D | observations | never above | wide-cycle only |
|---|---|---|---|
| 2 | 1,638 | 69.0% | 45.4% |
| 5 | 984 | 79.3% | 61.1% |
| 8 | 368 | 78.8% | 70.5% |
| 15 | 28 | 57.1% | 60.0% |

Peaks at ~79% and falls again. One in five recovers; two in five in
wide-stop cycles. No one-way door.

**As a rule: rejected at every level.**

| D | all cycles | vs base | times fired |
|---|---|---|---|
| 3 | 32.52% | −7.61 | many |
| 5 | 35.37% | −4.77 | many |
| 8 | 38.42% | −1.71 | 51 |
| 10 | 38.59% | −1.54 | 30 |
| 12 | 38.38% | −1.75 | 18 |
| 15 | 37.75% | −2.38 | 7 |
| 18 | 40.13% | 0.00 | **1** |
| 25 | 40.13% | 0.00 | **0** |

Above D=15 the rule runs out of events — only 7 of 150 stock-cycles ever
fall 15% from their own peak, and the deepest in the history is −20.9%. The
zeros at D≥18 are an empty set, not a result.

**Structural reason.** With a stop 5–10% below *entry*, a stock must rise a
long way, then fall 15%+ from that high, while staying above the entry
stop. Few paths satisfy both.

---

# 4. Selection-rule experiments — all rejected

## 4.1 The v1.1 price-and-derivatives selector

**Idea.** Replace the volatility-led score with a blend of three
equal-weighted blocks:

- **Derivative** — mean z of roll surprise, carry, OI change, futures volume
- **Volatility** — *negative* mean z of ATR20/price and HV20, i.e. reward CALM
- **Trend** — 0.50·z(60D) + 0.30·z(20D) + 0.20·z(5D)

Mandatory close above the 20 and 50 DMA, max 3 per sector, top 10 from a
top-50 candidate list.

**Result: −24.93pp — the worst single change tested.**

| entry | exit | sum | sd | worst | best |
|---|---|---|---|---|---|
| V4 | V4 | +34.02% | 7.47 | −6.18% | +19.84% |
| v1.1 | V4 | +9.09% | 3.79 | −5.64% | +5.80% |
| v1.1 | ATR | +0.33% | 3.49 | −4.53% | +5.19% |

**Two structural faults.**

*The volatility term is inverted relative to what works.* Standard
deviation halves (7.47 → 3.79) and so does the return. It is not reducing
risk — it cuts off the right tail that carries the strategy. V4's two
biggest months, Jan-26 +13.70% and Mar-26 +19.84%, returned +0.83% and
+5.80% under v1.1.

*The DMA gates bind hardest when they cost most.* In Mar-2026 breadth was
19.4% and only **7 names in the entire universe** cleared both DMAs, so
v1.1 produced a 7-stock portfolio. The filter empties the pool right after
a crash — exactly when the rebound happens.

## 4.2 The 2×2 matrix

Both changes independently and together, 13 cycles, fresh start:

| entry | exit | sum | mean | sd | worst | positive | t |
|---|---|---|---|---|---|---|---|
| **V4** | **V4** | **+34.02%** | 2.62% | 7.47 | −6.18% | 8/13 | 1.26 |
| V4 | ATR SOP | +18.66% | 1.44% | 7.92 | −6.09% | 7/13 | 0.65 |
| v1.1 | V4 | +9.09% | 0.70% | 3.79 | −5.64% | 8/13 | 0.66 |
| v1.1 | ATR SOP | +0.33% | 0.03% | 3.49 | −4.53% | 6/13 | 0.03 |

Swapping the exit costs −15.36pp, the entry −24.93pp, both −33.69pp.

## 4.3 The 52-week-high drawdown filter

**Idea.** V4 keeps re-selecting broken charts because a large drawdown
*raises* realised volatility, which carries 0.50 weight. On the 28-Jul-2026
expiry **6 of 10 names repeated** from a July basket that lost 2.76%, and
TRENT ranked #1 while 52% below its 52-week high and under its 50-DMA. So:
exclude anything more than X% below its 52-week high *before* scoring.

**Result: rejected at every threshold.**

| floor | sum | mean | worst | positive |
|---|---|---|---|---|
| **none (base)** | **+38.97%** | 3.00% | −4.25% | 9/13 |
| 30% | +29.93% | 2.30% | −4.51% | 8/13 |
| 40% | +33.85% | 2.60% | −4.25% | 7/13 |
| 50% | +31.67% | 2.44% | −4.25% | 8/13 |

**It failed exactly where predicted.** Jan-2026 drops from +12.56% to
+7.49% at a 30% floor — a rebound month bought when the market was crushed,
so the best names were far below their highs *by construction*.

Names removed were consistent: IDEA, PGEL, KAYNES, INDUSINDBK, ADANIGREEN,
CONCOR. Broken charts — which is what the volatility-led score is
*supposed* to buy.

**Conclusion: the repeat-buying pattern is a feature, not a bug.**

## 4.4 Earlier selection experiments (pre-03-Aug)

Measured with the `sim()` helper described in 8.1, so levels are
unreliable; rankings probably hold. All lost to unchanged V4.

| change | ₹100 over 5 cycles |
|---|---|
| LLM per-stock targets | 104.49 |
| volatility centring (Gaussian at universe median) | 103.75 |
| price-momentum selection | 102.13 |
| sector-first selection (best of 5 variants) | 107.85 |
| **live V4** | **115.90** |

The Gaussian reproduced the comparison portfolio's volatility profile
almost exactly (25.9 vs 28.2, 33.3 vs 32.9) and still shared only 2 of 49
picks — matching a summary statistic says nothing about selection.
Sector-first got *worse* the more it concentrated (3 sectors 98.65, 10
sectors 107.85), so its only benefit was diversification.

### Why the LLM target layer failed, precisely

The prompt asked for a level "reachable in 21 sessions" and filled the
context with caution language — resistance, extension, drawdown. Every
instruction pushed the answer DOWN and nothing said that setting it too low
permanently forfeits the upside. Mar–Apr 2026: eight positions booked at
10–14% while the same names finished at +12.9, +12.7, +11.3, +19.3, +30.0,
+50.5, +50.6. **7 of 8 kept running.**

The flat 40% is not a profit-booking rule, it is a **tail-risk cap**. Firing
twice in thirteen months is the design working. A tight target is
incompatible with a high-volatility selection.

---

# 5. Regime and timing experiments — all rejected

## 5.1 Index above its own 200-DMA instead of breadth

**Idea.** Replace the hardcoded 45% threshold with the standard trend
filter — is an equal-weighted index of the universe above its own 200-day
average? The crossover *is* the threshold, so no arbitrary number.

**Result: 12/13 correct vs breadth's 13/13. Rejected.**

Apr-2025 breaks it: the index sat at 0.979 of its 200-DMA, still recovering
from the March crash, so it called the wide stop when tight was right.
**Breadth recovers faster than the index** because it counts participation,
not capitalisation. The recognised standard filter is a cycle late.

Also tested: moving the threshold 45% → 50%. Fails — Apr-2025 at 45.5%
flips to wide and loses that month too.

## 5.2 Buffered breadth threshold (hysteresis)

**Idea.** Two lines instead of one: below 40% go wide, above 50% go tight,
in between hold last month's setting, so a marginal reading cannot flip the
stance.

**Result: 11/13 vs 13/13. Rejected.** Gets Apr-2025 wrong (45.5% sits in
the dead zone, holds the previous wide stop) and Jan-2026 wrong (40.0%,
dead zone again, holds tight).

Worth remembering anyway: 13 observations fitting one threshold is thin,
and Feb-2026 cleared 45% by only 3 points.

## 5.3 Weekly breadth de-risk trigger

**Idea.** Re-read breadth weekly. If it deteriorates, tighten every live
stop to 3% or flatten to cash. Two shapes: absolute floor, and a drop from
the entry-day reading.

**Result: best arm +39.75% vs base +39.57%. Rejected — 0.18pp is nothing.**

| arm | sum | weak-4 months | best-2 months | fires |
|---|---|---|---|---|
| base | +39.57% | −14.98% | +33.22% | 0 |
| DROP5 → 3% stop | +39.75% | −14.39% | +33.22% | 5 |
| DROP10 → cash | +36.08% | −15.41% | +33.22% | 3 |
| ABS40 → cash | +20.02% | −14.91% | **+20.00%** | 4 |

**The absolute floor destroys the strategy** — it flattens Jan-26
mid-cycle. Daniel & Moskowitz: momentum crashes are contemporaneous with
market *rebounds*, so panic and recovery are the same state and a
level-based rule cannot tell them apart.

**The DROP framing survives the good months** but does not pay, because
**the weak months were never breadth events**:

| cycle | entry breadth | weekly min | delta | return |
|---|---|---|---|---|
| 2025-06 | 63.3 | 56.8 | −6.5 | −3.20% |
| 2025-07 | 56.8 | 52.5 | −4.3 | −4.25% |
| 2025-12 | 55.1 | 41.5 | −13.6 | −3.51% |
| 2026-02 | 48.1 | 32.5 | −15.6 | −4.02% |

Jun-25 and Jul-25 — half the problem — never deteriorated more than 6.5
points. Dec-25 and Feb-26 did fall and the trigger fires, but **makes both
worse** because the damage is already done and it sells the bottom.

## 5.4 Does breadth deterioration predict the rest of the cycle?

**Idea.** Before asking whether to *act*, ask whether falling breadth
carries information at all. For every session in every holding window,
correlate breadth change since entry against the return still to come.

**Result: correlation +0.062 across 274 sessions. No signal — and the sign
leans the wrong way.**

| breadth change since entry | n | mean forward return | positive |
|---|---|---|---|
| fell >15pp | 7 | **+0.90%** | 7/7 |
| fell 10–15pp | 7 | **+0.79%** | 7/7 |
| fell 5–10pp | 28 | +1.55% | 22/28 |
| fell 0–5pp | 68 | +0.54% | 41/68 |
| flat or up | 164 | +0.61% | 76/164 |

**Every session where breadth had fallen more than 10 points went on to
make money — 14 for 14.** The flat-or-rising bucket is worst on median.

The three cycles where breadth *rose* during the hold returned +5.47%,
+12.13% and +22.90%. The two biggest collapses were Feb-26 (−29.1pp) and
Dec-25 (−15.6pp), but in Feb-26 the book was already at −4.12% four
sessions in, **before** breadth collapsed.

**Breadth is a lagging description of what prices already did.** Its one
useful role is the one it has: read once, at entry, to set stop width.

## 5.5 Day-on-day drop thresholds

**Idea.** Is there a single-day portfolio fall after which the cycle never
recovers? "2–3% is AC ripple, 7% is a DC level change."

**Result: no such level, and the data leans the other way.**

15 cycles, 99 down days with ≥5 sessions of runway:

- largest drop that **did** recover: **−4.45pp**
- smallest drop that **never** recovered: **−0.04pp**

Failure rate: 51% at ≥0.5pp, 50% at ≥1pp, 33% at ≥2pp, **0% at ≥3.5pp**.
A coin flip everywhere, then perfect recovery at the extreme. Apr-2025 took
−4.26pp and −4.45pp within four sessions and still climbed to +2.23%.

**There has never been a 5% down day.** A ten-name equal-weight book with
5% stops cannot produce one — the stops truncate exactly the tail you would
want. Individual stocks gap 6–8%; one name is 10% of the book.

Drawdown from peak, same answer: a **−8.72% drawdown fully recovered**, and
the failure rate peaks near 80% at 3–4% then collapses to zero past 6%.

## 5.6 Universe-median 60-day return as a regime signal

Of nine market-wide aggregates tested at entry against the cycle's forward
return:

| metric | correlation with forward return |
|---|---|
| **universe median 60-day return** | **−0.73** |
| universe median 20-day return | −0.64 |
| % above 20 DMA | −0.50 |
| median HV20 | +0.43 |
| median cost of carry | −0.34 |
| median rollover | +0.08 |
| median roll surprise | −0.02 |

The more the market has already run, the worse the next month. Three of
four weak months followed a rising market; both blowout months followed
heavy declines. Consistent with Daniel & Moskowitz, and it explains *why*
breadth works — breadth is a proxy for the same thing.

**Tested as a rule: rejected.**

| rule | sum |
|---|---|
| base (breadth regime) | +39.57% |
| cash when median 60D > 6% | **+45.53%** |
| cash when median 60D > 4% | +41.87% |
| cash when median 60D > 8% | +41.27% |
| stop width from median 60D instead of breadth | +33 to +39% — all worse |

The +45.53% is not real. The threshold curve is **jagged** (42.92 → 39.41 →
41.87 → **45.53** → 41.27) where a genuine signal degrades smoothly, and
**the entire gain is two months**: at X=6 it sits out Jun-25 (−3.20%),
Jul-25 (−4.25%) and May-25 (+1.50%), netting +5.96pp — exactly the gap.

**Status: the most promising unproven idea here.** It has a mechanism,
agrees with breadth in sign, and is the best reason to extend history to
2018.

---

# 6. Comparison against an external portfolio ("Altcase")

A subscription service publishing a monthly ten-stock basket, used as
benchmark and reverse-engineering target.

## 6.1 Return comparison, 12 common months

| | sum | negative months | sum of negatives | best month |
|---|---|---|---|---|
| Altcase | +54.87% | 1/12 | −1.22% | +9.74% |
| V4 | +36.66% | 4/12 | −14.98% | +22.90% |

**68% of the gap is our four losing months.** We do not lose on the upside
— our best month is more than double theirs. They have almost no left tail;
we have a much better right tail.

## 6.2 Where their edge comes from — a finding, then a correction

**First conclusion (Mar-2026):** their entry prices sat a median **3.9%
below** ours on identical names — limit orders under the market, filling on
dips. 27 of 29 entries were below the next-day open.

**Correction (Jul-2026):** they entered **at** the 1-Jul open, mean −0.04%
versus ours. No entry edge at all.

The Mar-2026 comparison was probably measuring their recommendation date
against our expiry+1 — **two different days**. That analysis needs redoing
against their stated issue dates before it is trusted.

## 6.3 Selection versus exits, isolated (Jul-2026)

Their basket through our exit rules, identical window:

| | return |
|---|---|
| their picks + their exits | +3.07% (reported) |
| their picks + **our** exits | +2.95% |
| **our picks** + our exits | −2.76% |

**Exit rules are worth 0.12pp. The entire 5.83pp gap is stock selection**
that month. Zero overlap; theirs liquid large/mid-caps, ours high-beta
small/mid-caps.

Their target also costs them upside: ETERNAL made +16.37% under our 40% cap
versus the +7.40% they banked.

## 6.4 Overlap with their picks

| selector | overlap |
|---|---|
| V4 (derivatives-led) | 8 of 115 names — **7.0%** |
| v1.1 (calm/trend blend) | 24 of 115 — **20.9%** |
| PICKS method, judgement-assisted | 8 of 40 — **20.0%** |

v1.1 tripled the name-matching **and returned +0.33% against V4's +34.02%**.
Getting closer to their basket did not get closer to their returns.

## 6.5 Their method, from their published weekly report

- Universe is **NIFTY 500**, not F&O. Of 38 weekly shortlisted names, 16
  are cash-only — though their *monthly* baskets were all F&O-reachable.
- Core signal is a **discrete trend state** (Strong/Mild Bullish), not a
  continuous score, with a "holds above" support level and a target.
- Average target upside **~9%**, matching the +9.7% median reverse-engineered.
- They **rotate by sector**, leading with a ranked sector heatmap.
- They watch **FII/DII flows** — data we do not use.
- Small caps come from a **volume-surge scan** (+100% to +3,500% W-o-W).

## 6.6 Their regime switch — unexplained

Profiling their own picks on the selection date:

| month | their 10 above 20+50 DMA | median 20D | universe median 20D | median from 52w high |
|---|---|---|---|---|
| Jul-26 | 10/10 | +13.53% | +1.74% | −1.4% |
| Feb-26 | 7/10 | +6.54% | −3.68% | −5.5% |
| Mar-26 | 4/10 | −0.62% | +2.11% | −20.0% |
| Apr-26 | 2/10 | −7.91% | −12.58% | −22.5% |

In July they bought leaders **at** their highs. In April, wreckage 22%
below highs — but names falling *less* than a universe down 12.58%.
**Feb and Mar contradict any simple market-state rule**: Feb had a weak
tape and they bought strength; Mar had a rising tape and they bought
weakness.

---

# 7. The judgement-assisted PICKS method

A separate Claude instance ran `PICKING_METHOD.md` blind — price-and-volume
selection, regime-classified TRENDING / MIXED / DAMAGED, with a judgement
overlay — across Feb, Mar, Apr and Jul 2026.

## Overlap with Altcase

| month | before a data fix | after |
|---|---|---|
| 2026-02 | 2/10 | 1/10 |
| 2026-03 | 0/10 | 0/10 |
| 2026-04 | 2/10 | 1/10 |
| **2026-07** | 2/10 | **6/10** |
| total | 6/40 | **8/40** |

**July hit 6/10 — the target — matching an independent weight-grid search
for that month exactly.** Two unrelated methods converging says the
trending branch is real.

**March scored 0/10 twice.** The rules classify it TRENDING (median 20D
+2.11%, 41.75% above both DMAs) so it buys momentum; Altcase bought
beaten-down solar. The regime label is wrong for that month.

## Return profile, our exit rules, identical windows

| month | PICKS | V4 | diff |
|---|---|---|---|
| 2026-02 | +8.33% | +10.40% | −2.07 |
| 2026-03 | −4.39% | −2.55% | −1.84 |
| 2026-04 | +17.08% | +19.61% | −2.53 |
| 2026-07 | +0.68% | −2.76% | **+3.44** |
| **sum** | **+21.70%** | **+24.70%** | −3.00 |

Altcase's reported total for the same four months: **+18.62%**. Both of
ours beat theirs.

PICKS loses in three months by a consistent 1.8–2.5pp and wins in the one
month where overlap hit 6/10.

**Conclusion: the binding constraint is the regime classifier, not the
ranking weights.**

---

# 8. Bugs found on 03-Aug-2026, and what they changed

## 8.1 The `sim()` helper silently dropped carry-forward

An ad-hoc helper reimplemented the monthly loop instead of calling
`strategy.simulate_month`, omitting carry-forward and chaining. It reported
live V4 at **₹115.90** over 5 cycles when the correct figure was
**₹131.99**. Every comparison scored against it used a baseline understated
by ₹16 — which *flatters* the challengers, and they still lost.

Seven forked backtest scripts were deleted; `research/harness.py` now
delegates every walk to `simulate_month`.

## 8.2 Corporate actions never applied to the holding window

`split_adjust` only cleaned the volatility lookback. The OHLC positions were
walked against was raw, so **BSE's 2:1 on 23-05-2025 read as a −61.99%
crash**, tripped the stop, and cost the Apr-2025 cycle **6.2pp**. Live, the
same event fires a false EXIT alert on a stock that merely split.

## 8.3 Stops could not lose more than their width

`simulate_month` filled at the stop price on any intraday touch — correct
when the level trades, wrong when the session **gaps through it**. TRENT
closed ₹3,343.80 on 06-07-2026 and opened ₹3,080.00 against a ₹3,120.75
stop: a −6.24% fill, not −5.00%.

**Combined effect of 8.2 and 8.3: +39.34% → +36.87%** (carry-forward
convention). Gap-through fills cost more than the split guard saves.

## 8.4 The evening report tracked a portfolio nobody owned

`daily_report.build` ran the surveillance veto **after** the simulation and
discarded the result. The note followed KALYANKJIL (ASM Stage I, vetoed out
of the basket actually sent) while the real book held ADANIGREEN. Damage
that cycle: 0.01pp, by luck.

## 8.5 Three places derived a basket, only two applied the veto

`daily_report.build`, `daily_report.build_entry_sheet` and
`run_strategy.cmd_basket` each derived the ten names independently. All
three now call `strategy.basket_for`.

## 8.6 `build_entry_sheet` could not run at all

Referenced `rpt`, `merged`, `days`, an unimported `llm_judgment` and an
undefined `universe_stats`. Every call raised NameError — the monthly order
sheet was unproducible.

## 8.7 `from52wh` was fooled by bonuses

BAJFINANCE read **−87.9%** below its 52-week high after a 10:1 on
16-06-2025. True figure: **−0.9%**.

## 8.8 A 404 was cached as permanent

NSE publishes the bhavcopy around 18:00 IST. A run before that got a 404
and recorded it forever, so the file was never re-fetched. On 04-08-2026
two runs at 01:05 and 01:07 poisoned the date and every evening run
reported "not a trading day". Markers are now only trusted for dates older
than four days.

---

# 9. What has NOT been tested

- **Limit-order entry.** Bidding ~2% below the expiry close instead of
  taking the open. The one mechanism with external evidence behind it,
  never tested on our basket — though 6.2 weakens that evidence.
- **Extending history to 2018.** Highest-value task. 13 observations cannot
  support the parameters already in use, let alone new ones. Would settle
  the median-60D signal (5.6) and the 45% threshold.
- **Position sizing.** Barroso & Santa-Clara volatility scaling — hold less
  in high-volatility regimes — is the best-supported answer in the
  literature to this exact problem. Every experiment here held ten names at
  10% each.
- **The ±10% F&O price band in the backtest.** Targets currently fill on any
  day whose high touches them, impossible below about +27.3%.
- **The four `sim()`-era rejections** (4.4) re-run through the real engine.

---

# 10. Summary

| category | tried | adopted |
|---|---|---|
| exit rules | 6 | 1 (regime-pegged stop) |
| selection rules | 7 | 0 |
| regime / timing | 6 | 0 |
| **total** | **19** | **1** |

Everything that failed did so for one of three reasons.

**Cutting risk cuts the right tail.** The strategy earns from a small number
of large months. Calm-volatility selection, drawdown filters, trailing stops
and de-risk triggers all reduce variance and reduce return by more.

**The signals lag the book.** Breadth, drawdown and day-on-day moves all
describe damage after it has happened. Ten of the most volatile names in the
market move before the median stock does.

**Panic and recovery are the same state.** Momentum crashes coincide with
market rebounds. Any rule that flattens on weakness sells the setup for the
best months — Mar-25, Jan-26 and Mar-26 account for most of the return, and
every de-risk rule tested damages at least one.

The one adopted change works because it does the opposite: it *widens* the
stop when the market is beaten down.

**Live config unchanged. +39.57% over 13 cycles, t 1.57 — a hypothesis
under live test, not an established edge.**

## Literature this is consistent with

- Daniel & Moskowitz, *Momentum Crashes* (JFE 2016) — crashes occur in
  panic states, after market declines and in high volatility, and are
  contemporaneous with rebounds.
- Barroso & Santa-Clara (2015) — scaling by trailing realised volatility to
  a constant target virtually eliminated crashes and nearly doubled Sharpe.
- Han, Zhou & Zhu, *Taming Momentum Crashes* — a 10% stop cut maximum
  monthly loss from −49.79% to −11.36% and more than doubled Sharpe; and
  **transaction costs erode tight stops while wide ones survive**. Our own
  sweep independently reproduced this: 10% was the best fixed width
  (+32.08%), 3% the worst (+15.38%).

---

# 11. V5 — "basket actualization" (12-Aug-2026)

Everything above assumes perfect execution: every name in the basket fills
at the session open, on day one, no exceptions. `research/fill_realism_v5.py`
tests what happens when that assumption is replaced with a realistic,
multi-day fill process — same V4 basket (picks, sector caps, regime stop),
different execution layer. Delegates to `strategy.simulate_month` throughout
via its new `entry_overrides` parameter; nothing forks the loop.

## Why this started

A day-1-only fill check (band limit priced at the volatility band's low
edge) found **38.8% of names (62/160) missed their day-1 fill** across 16
cycles (Mar-25–Jun-26) — every single miss because the stock gapped past
the limit at the open itself, never an intraday-range miss. Zero of the 16
months had a fully complete 10/10 basket on day 1.

## The mechanism (final, validated version)

- **Day 1:** standard 20-day volatility band, whole-share sizing solved
  against the priciest stock's own band low × 10 slots (this IS the
  minimum feasible basket size, by construction — see
  `daily_report._compute_min_portfolio_sizing`). Real fill if that day's
  low ≤ the quoted limit.
- **Day 2** (day-1 misses only): the 20-day band is abandoned — it
  demonstrably failed. An 80%-probability opening price is computed from
  Day-1's own realized volatility (Parkinson estimator off that single
  day's H/L), anchored off Day-1's close. Shares resolved fresh against
  it. Realized fill rate came in at 98.4% (61/62), well above the 80%
  target — the single-day Parkinson estimate runs wide relative to actual
  gap risk, which favours completeness over price precision.
- **Day 3** (day-2 misses only, 1 name in 160 ever reached this stage):
  pools Day-1 and Day-2's realized vols for a steadier estimate, computes
  an 80%-probability opening price, and **decides share count from that
  the evening before** (no lookahead at the real Day-3 price — an earlier
  version of this backtest picked shares after seeing the actual Day-3
  price, which is not achievable live and was corrected). Executes at
  Day-3's actual market open, no limit — the basket must be complete.

## The risk-anchor fix (the one that mattered most)

First pass anchored each position's stop/target to wherever it actually
filled. Two names (PNBHOUSING, -1.34pt on the month; BSE, -2.21pt) flipped
from large winners under perfect execution to stopped-out losses under
realistic execution — not because of the delayed entry's price alone, but
because a later, higher fill dragged the stop UP with it, so a move that
wouldn't have troubled the original entry did trigger from the higher one.

Fix: stop and target are now **always** computed off Day-1's actual market
open — the "arrival price" / decision price in Perold's (1988)
*Implementation Shortfall* framework — never off wherever the delayed fill
actually happened (`strategy.simulate_month`'s new `open_position(...,
risk_anchor=...)` parameter, threaded through `entry_overrides`).

## Gap-risk abort

Before attempting Day 2 or Day 3, or executing the Day-3 forced buy, the
preceding day's low (or Day 3's own open) is checked against the
anchor-based stop. If price has already gapped through it, the entry is
**aborted** — the slot stays in cash for the month, no fallback fill,
never backfilled from a lower-ranked name. Gap risk is a documented,
named failure mode: ordinary stop-loss orders "cannot protect you from
overnight gaps" and a gap-through fill executes "at a much worse price
than intended" — the abort exists so the strategy never pays to enter a
position that's already through its own risk boundary. Triggered 0/160
times in this sample; the protection is real but did not bind historically.

## Results — 16 cycles, Mar-2025 to Jun-2026

| | |
|---|---|
| Baseline (perfect execution) additive sum | +36.24% |
| V5 (realistic fill chain) additive sum | **+36.72%** |
| Delta | **+0.47pt** over 16 months (+0.03pt/month) |
| Months V5 beat baseline / worse | 8 / 8 |
| Worst month | -1.83pt (2026-01) |
| Best month | +1.95pt (2026-03) |

Fill-day distribution (160 name-months): **61.3% day 1, 38.1% day 2, 0.6%
day 3, 0% aborted.**

Weight precision vs the 10% equal-weight target (measured against realized
invested value, real execution prices): **mean |deviation| 0.40pt, 148/160
(92.5%) within ±1pt, max deviation 2.97pt.**

**Conclusion: with the risk-anchor fix and gap-abort rule in place, the
realistic multi-day fill chain costs effectively nothing relative to the
idealized backtest (+0.47pt net over 16 months) while getting every name
into the basket within two days in 99.4% of cases.** Without the
risk-anchor fix, the same fill chain cost -4.27pt over the same 16 months
— the anchor was the load-bearing piece, not the multi-day chain itself.

## Literature this is consistent with

- Perold, *Implementation Shortfall: Paper vs. Reality* (Journal of
  Portfolio Management, 1988) — defines the arrival/decision price as the
  correct execution-independent benchmark; the gap between a "paper"
  portfolio priced at decision time and the real portfolio is a single
  measurable cost with a delay component.
- Daniel & Moskowitz, *Momentum Crashes* (JFE 2016) — momentum portfolios
  carry real, forecastable crash risk concentrated in sharp reversals;
  consistent with treating a name that's already gapped through its own
  risk boundary before entry as a broken setup, not noise to buy through.
- Standard retail/futures execution literature on gap risk — stop orders
  are explicitly documented as unable to protect against overnight gaps,
  which motivated the abort rule rather than trusting the stop to catch it
  after the fact.

## Data

Per-month, per-stock detail (fill day, price, shares, weight, deviation,
abort reason where applicable) in `data/fill_realism_v5.jsonl`, one JSON
line per cycle. Reproducible via `python research/fill_realism_v5.py
<year> <month>`.

# 12. V5 simplified to 2 stages (13-Aug-2026)

## Why this changed

Section 11's V5 used a 3-stage fill chain (Day-1 limit, Day-2 limit retry
off Day-1's own volatility, Day-3 forced market buy). Building the actual
investor-facing `entry_tracking.py` message against that chain against the
agreed message cadence — Day 0 quote, Day-1-eve update, Day-2-eve **final**
fill list, Day-3-eve repeat, Day-4 normal service — didn't fit: there is no
message slot left for a *third* trading day's action. The cadence only
allows for two trading days before the basket must be complete.

The chain was cut down to match: **Day 1 is the only limit attempt; Day 2
is a mandatory, unconditional market buy for anything Day 1 missed.**
Day 2's share count is still decided the evening before (Day-1 close, no
lookahead), using the same 80%-probability Parkinson estimate section 11
validated — it's just no longer offered to the market as a limit, since
Day 2 must clear the basket. The risk-anchor fix (stop/target always off
Day-1's actual open) and the gap-abort rule (never buy through an
already-broken stop) are unchanged from section 11.

## Results — same 16 cycles, Mar-2025 to Jun-2026

| | |
|---|---|
| Baseline (perfect execution) additive sum | +36.24% |
| V5 (2-stage realistic fill chain) additive sum | **+36.93%** |
| Delta | **+0.69pt** over 16 months (+0.04pt/month) |
| Fill-day distribution (160 name-months) | 61.3% day 1, 38.1% day 2, 0.6% aborted |
| Weight precision vs 10% target | mean \|deviation\| 0.45pt, max 2.95pt |

The 2-stage version is marginally *better* than the 3-stage version
(+0.69pt vs +0.47pt net), not worse — collapsing the old day-2-limit-retry
and day-3-forced stages into a single mandatory day-2 buy means names that
would have missed a second limit attempt get into the basket a day
earlier, at whatever day-2's open turns out to be, rather than waiting for
a third day. Aborts stayed at 1/160 (0.6%), same order as before.

The old 3-stage numbers and per-stock detail were preserved at
`data/fill_realism_v5_3stage_backup.jsonl` for reference; the file used by
the live `entry_tracking.py` module (`data/fill_realism_v5.jsonl`) then
held the 2-stage results, reproducible via `python
research/fill_realism_v5.py <year> <month>`.

**STALE as of 25-Aug-2026 -- see section 16.** `entry_tracking.py` was
reverted to 3 stages the very next day (14-Aug-2026) and this section was
never updated to say so; `research/fill_realism_v5.py` and its data files
modelled this now-abandoned 2-stage design and have since been deleted as
dead code. The mechanism actually live today is 3-stage, backtested by
`research/fill_realism_v6_3stage.py` / `data/fill_realism_v6_3stage.jsonl`.

# 13. Carry-forward with HOLD rebalancing vs pure in-and-out, compounding (13-Aug-2026)

## What this tests

Section 12's V5 numbers are additive, independent cycles (fresh capital
every month) -- correct for validating the fill mechanism on its own,
but not comparable to a real portfolio that carries positions between
months. This section chains BOTH the pure in-and-out approach and the
new carry-forward + HOLD-rebalance mechanism (see daily_report.
_slot_target_from_holds / _compute_hold_rebalance, agreed 13-Aug-2026)
into one whole-share, compounding NAV each, over the same 16 real
cycles, so the two are directly comparable. Reproducible via
`python research/carry_forward_v5.py`; full monthly detail in
`data/carry_forward_v5.json`.

Both scenarios use the same V5 2-stage fill mechanism (section 12) for
every fresh buy and the same `strategy.simulate_month` engine for every
stop/target/rollover exit -- they differ ONLY in whether a name that
repeats in next month's basket is sold and rebought (pure in-and-out) or
held and, if it has drifted outside +/-10% of the current slot target,
trimmed (carry-forward).

## A real bug found and fixed in strategy.simulate_month while building this

The slot-assignment counter treated a ROLLOVER sale (a carried position
dropping out of the new basket) as permanently consuming a slot number,
even though redeployment is off by default and that slot goes straight
to cash for the rest of the month. In a month with several carry-in
rollovers, this silently starved the entry_overrides fill loop of slot
numbers: 2 continuing holds + 4 rollovers used 6 of 10 slot numbers
before a single fresh buy was attempted, so 4 of 8 needed buys were
dropped with no exit, no position, and no error -- ~Rs 56,000 of one
month's capital simply vanished from the simulation. Fixed by tracking
actual free slot indices (`free_slots`, a pool) instead of a monotonic
counter; a rollover's slot is now correctly returned to the pool for the
SAME month's fresh fills. `tests/` re-run clean after the fix (56
passing, 2 pre-existing unrelated failures). This is a live bug in the
engine `daily_report`/`cmd_daily` also depend on -- worth being aware a
month with enough simultaneous rollovers could previously have
under-counted holdings in the daily note too, though it was never
reported live since it needed carry_forward + a slot-count squeeze that
this multi-month backtest was the first thing to trigger.

## Results -- same 16 cycles, Mar-2025 to Jun-2026, compounding NAV

| | |
|---|---|
| Pure in-and-out: seed Rs 1,29,565 -> final | Rs 1,69,991 (+31.20%) |
| Carry-forward + HOLD rebalance: seed Rs 1,29,565 -> final | Rs 1,81,462 (+40.05%) |
| Delta | **+8.85pt over 16 months in favour of carry-forward** |

Carry-forward wins in most months (11 of 16), loses in months where a
carried name that would have been re-bought fresh actually did better
starting from a clean entry that month (transaction-timing luck, not a
mechanism flaw). The advantage compounds mainly through two channels:
avoiding the round-trip fill friction of selling and rebuying a repeat
name every month (each round-trip risks a worse V5 Day-2 fill on the
way back in), and letting a position's cost basis run instead of an
artificial monthly reset.

## Caveat

16 months, one basket-selection path, one market regime -- same
small-sample caveat as every other number in this log. The size of the
advantage is not something to trust to the percentage point; the
DIRECTION (carry-forward beats reset-every-month) and the ORDER OF
MAGNITUDE (high single digits to low double digits of points over 16
months) are the load-bearing findings here.

# 14. Corporate-action bug in the whole-share book ledger (14-Aug-2026)

## What broke

`strategy.simulate_month`'s internal walk already neutralises a split or
bonus (`adjust_holding_window`, built for the BSE 23-May-2025 2:1/~3:1
event) by scaling PRICES forward from the action date so a FIXED share
count stays value-consistent on the pre-action basis -- correct for its
own percentage-return bookkeeping. `research/carry_forward_v5.py`'s
separate whole-share `book` ledger (built in section 13, real money,
real tradeable shares) never adjusted its share COUNT for the same
event, while still pulling `simulate_month`'s internally-adjusted price
for month-end marking. The two errors happened to cancel out in the
SAME cycle's own NAV mark (2 pre-split shares x an inflated pre-split-
equivalent price = the same real value as 6 post-split shares x the
real post-split price), which is why 16 months of NAV totals looked
unremarkable. The bug surfaced downstream: every LATER cycle's
rebalance/trim step and the New-vs-Existing basket comparison (section
17 upstream) combine `book`'s share count with RAW, unadjusted market
prices sourced directly from bhavcopy -- and there, the un-adjusted
share count (2, should have been 6) understated the position by exactly
the split ratio. Found via a user-flagged inconsistency: the
New-vs-Existing table showed different Existing totals for consecutive
months at an unchanged scale=1.000, traced to BSE's April-2025 carry
mismarking as 2 shares at Rs 7,417.51 (the internal, pre-split-
equivalent price) instead of 6 shares at the real 30-May-2025 raw open
of Rs 2,472.50.

## Fix

Two changes, both additive/backward-compatible:

1. `strategy.adjust_holding_window` gained `return_factors=True`, which
   also returns `{symbol: cumulative_price_factor}` for whatever it
   detected over the window (empty dict when nothing breached).
   `simulate_month` now always requests this and exposes it as
   `MonthResult.corp_action_factors`.
2. `carry_forward_v5.run_scenario` now, immediately after each cycle's
   `simulate_month` call (and after that cycle's own exits are
   resolved), multiplies `book[sym]` by the detected factor for any
   symbol still held -- moving the share count from the internal
   pre-action basis onto the real, current-market basis. Month-end
   marking (`mark_value`, `alloc["final_book"]`) was switched from
   `simulate_month`'s internally-adjusted `.entry` to `merged`'s RAW,
   unadjusted open price at the same date (`merged` is this script's own
   copy and is never mutated by `simulate_month`'s internal call, which
   always returns a new dict) -- so `book`'s share count is now always
   paired with a real, quotable price, both this cycle and every
   cycle after.

## Verification

Re-running the 16-cycle carry-forward backtest with the fix found
exactly one corporate action across the whole period -- BSE, April-2025
cycle, factor 3.0 (`book['BSE']` 2 -> 6 shares). Every other month's
`corp_action_factors` was empty, and NAV/return totals through the
un-affected months matched section 13's numbers unchanged (confirming
the cancellation reasoning above -- this cycle's OWN mark was already
right; only what carried into LATER months needed correcting). The
May/June/July-2025 New-vs-Existing detail tables now show BSE as 6
shares at Rs 2,472.50 (May, HOLD) / 4 shares at Rs 2,775.91 (June, fresh
BUY) / 1 share at Rs 2,429.30 (July, fresh BUY) -- all real, current
prices, no internal split-adjustment leaking into a whole-share ledger
again.

# 15. Holds are never sold for rebalancing -- coverage-scale top-up replaces trim-only (14-Aug-2026)

## What changed and why

Section 13's carry-forward mechanism trimmed an overweight hold and
never topped up an underweight one -- "we will never need to buy into an
existing position" (13-Aug-2026). Explicit correction, same day as
section 14: "OUR PUREST STRATEGY WAS IN AND OUT 100%... we said we will
not sell held stocks but nobody said we can't buy more." The trim-only
asymmetry is retired. A hold is never sold for rebalancing again in
either direction -- not trimmed for being overweight, not left
underweight either. Only a genuine stop/target/rollover exit
(unchanged, `strategy.simulate_month`'s own job) still sells a hold.

## The new construction

1. Build a fresh, +/-10%-consistent minimum basket exactly as a
   brand-new investor gets: priciest pick's own band-low sets the slot,
   every name in the FULL basket (holds + fresh picks) solved to whole
   shares against it (`daily_report._compute_min_portfolio_sizing`).
2. Coverage scale: `k = max(held_shares / this-basket's-own-share-count)`
   over every hold. Scaling the whole basket by this k guarantees, by
   construction, that every hold's target share count is >= what's
   already held -- found the RATIO matters here, not the raw share
   count: IDEA (386 held, cheap, so a huge raw count) did not bind the
   September-2025 cycle, KAYNES (expensive, base count 1) did.
3. Buy-feasibility floor, now applied across ALL ten names (holds
   included, was fresh-buys-only before): if any single name's own
   band-low still doesn't fit inside the coverage-scaled slot, that
   price becomes the floor instead. Safe regardless of order -- raising
   the slot only ever grows every name's share count, so it can only
   make the coverage guarantee in step 2 MORE generous.
4. Resolve whole shares for the full basket against the final slot
   (`daily_report._resolve_shares_to_target`), then a belt-and-suspenders
   clamp: a hold's target never falls below what's already held.
5. Execute: every hold is topped up to its target with an unconditional
   Day-1 MARKET buy -- no limit chase, no gap-risk-abort check. A
   top-up isn't a new position (the hold already carries full exposure
   to that name's moves), so neither rationale that motivates the V5
   2-stage chain for a genuinely fresh buy applies. Buying at Day-1's
   open also matches `simulate_month`'s own carry-forward re-mark
   exactly -- `final` from last cycle IS this cycle's Day-1, so the
   already-held shares are already re-based to that same open. Old and
   newly-added shares land on one consistent price, same day, no
   blended cost basis. Fresh picks (no prior holding) still go through
   the unchanged V5 2-stage Day-1-limit/Day-2-mandatory chain.

`_slot_target_from_book_holds` (the 13-Aug-2026 interval-stabbing
target) is deleted, not just unused -- one construction, not two lying
around.

## Concrete effect -- September 2025, the case that surfaced the flaw

Old design: 6 holds stayed exactly where they were (never topped up),
4 fresh buys sized up to a slot inflated by KAYNES's own floor,
producing a basket where the buys sat at ~15.8-16.0% and the holds sat
at 4.5-7.2% -- badly lopsided, and KAYNES itself the max-weight name at
15.79% (the interval-stabbing table upstream had reported 16.68% for a
different month's case, POWERINDIA, same root cause).

New design, same cycle: every one of the 6 holds gets topped up
(IEX 20->49, MOTHERSON 27->64, IDEA 386->845, CONCOR 5->13,
PNBHOUSING 3->8, GLENMARK 1->4) alongside the 4 fresh buys, all ten
resolved against the identical Rs 6,877.35 slot. Every name lands
within 9.88-10.95% of weight -- no lopsidedness, nothing left
underweight, nothing trimmed.

## 16-month re-run

Re-ran the full 16-cycle backtest with the new construction (data/
carry_forward_v5.json regenerated). Final NAV Rs 1,99,862.66 vs. the
trim-only design's Rs 1,99,497.15 -- close, as expected (same
underlying strategy, same stop/target/rollover engine, only WHEN
capital gets committed to a hold changes). The real difference shows in
the per-month Existing-vs-New max-weight column: POWERINDIA-style
outliers (Oct/Nov-2025 previously reported at 16.12%/16.68%) now sit at
10.68%/10.30% -- the underweight-hold distortion that produced them is
gone, because holds no longer sit frozen below target while an
unrelated expensive fresh pick inflates the slot around them.

---

# 16. Real 3-stage fill mechanism backtested; live entry-sheet sizing
simplified; stale docs and dead research files cleaned up (25-Aug-2026)

## What triggered this

A real limit order (KPITTECH, then NATIONALUM, COFORGE, BANDHANBNK) was
quoted ABOVE the stock's own last close in a live sheet -- clearly wrong.
Root cause: `daily_report._solve_shares_to_slot` iterated its three
candidate share counts `{n-1, n, n+1}` via an unordered Python `set`; on
a near-tie in slot-fit deviation, whichever candidate the set happened to
iterate first won, sometimes the higher-priced one. **Fixed**: on a tie
(within 1e-6% deviation), prefer the lower price. A second, broader fix
was tried (capping the upper price bound at `close` rather than
`entry_hi`) and was WRONG -- section below quantifies why -- and was
reverted.

## Real returns were never actually measured against this

Every number in sections 11-15 above is an idealized backtest: every
name assumed to fill at the session open on day one. `entry_tracking.py`
(the module that actually talks to investors) has run a real 3-stage
process since the 14-Aug-2026 revert noted in section 12's stale-flag:
Day-1 20-day volatility band, Day-2 re-quote off Day-1's own realized
vol (only if Day-1 missed), Day-3 mandatory market fill anchored off
Day-2's close (only if Day-2 missed too), with risk_anchor always pinned
to Day-1's actual open regardless of which day the fill happened. No
backtest of this exact live mechanism existed -- `fill_realism_v5.py`
(section 11) modelled 3 stages but was superseded by a 2-stage version
(section 12) the live code no longer matches, and neither script's
sizing reflects tonight's own tie-break fix.

**`research/fill_realism_v6_3stage.py`** is a direct, formula-for-formula
port of `entry_tracking.py`'s current live mechanism into a backtest,
sizing Day 1 via `daily_report._compute_min_portfolio_sizing` (so it
carries the tie-break fix), delegating all P&L to
`strategy.simulate_month` via its existing `entry_overrides` parameter --
nothing here reimplements stops/targets/exits.

## Results -- 13 canonical cycles, Mar-2025 to Mar-2026

| | |
|---|---|
| Idealized (same-day-open) additive sum | +40.11% |
| Real 3-stage additive sum | **+39.20%** |
| Delta | **-0.91pt** over 13 months |
| Months real beat idealized / worse | 6 / 7 |
| Fill-day distribution | 48.5% day 1, 50.8% day 2, 0.8% day 3, 0% aborted |

Extending to all 17 cycles run so far (adding Apr-Jul 2026): idealized
+40.50%, real +40.16%, delta **-0.34pt**, 8 better / 9 worse. The gap is
real but small and two-sided -- this is Perold's (1988) implementation
shortfall, not a strategy defect: a name that misses its Day-1 limit
sometimes fills lower on Day 2 (helps) and sometimes higher (hurts), and
across 13-17 cycles the two roughly wash with a slight net cost.

**The reverted over-correction, quantified**: also capping the Day-1
limit's upper bound at `close` (not just `entry_hi`) was tried and
measured before being reverted -- it cost -2.6pt over the same 13 cycles
and dropped the Day-1 fill rate from 61% to 37%, because `entry_hi` is a
legitimate two-sided volatility-band estimate, not an arbitrary ceiling;
capping it just forced more names into the noisier Day-2 re-quote for no
benefit. Kept only the tie-break fix.

## The `cycle_state.py` / `book.json` disconnection (the real bug)

Separately, and more significantly: `cycle_state.py` (the daily P&L
tracker) and `daily_report.build()` (used to reconstruct the outgoing
month for SELL/HOLD/BUY classification) both independently assumed every
position entered at "the open of the first session after expiry" --
zero references to `book.py`/`entry_tracking.py` anywhere in
`cycle_state.py`, despite those modules already tracking the REAL fill
price/day via the 3-stage mechanism above. This affected every live
daily update and monthly performance number ever sent, not just
backtests. Fixed by making `book.json` the single source of truth
(seeded with all 10 basket names as blank/pending on expiry evening,
archived rather than deleted on close) and wiring both `cycle_state.py`
and `daily_report.build()` to read real entry price / risk_anchor from
it via `strategy.simulate_month`'s existing `entry_overrides` parameter
-- no new parallel mechanism. `config.ACTUAL_FILLS_FILE`, an earlier,
never-wired attempt at solving this same problem, was deleted.

## Live entry-sheet sizing simplified -- and now diverges from section 15

`daily_report.build_entry_sheet` (the live monthly order-sheet
generator) had the coverage-scale `k` inflation described in section 15
removed entirely, on explicit instruction: an existing investor's
CONTINUE TO HOLD line now shows only the plain natural minimum share
count (`_solve_shares_to_slot`'s own `n`, no basket-wide inflation), with
a "scale up as needed" disclaimer, instead of inflating the whole
basket's slot to cover whichever held position is most oversized. This
was necessary because the coverage-scale design could distort an
unrelated stock's fit -- e.g. one reconstructed high-share-count holding
pushed POWERINDIA from a correct 1 share to an incorrect 2.

**This means section 15's carry-forward backtest
(`research/carry_forward_v5.py`, still coverage-scale-k-based, untouched
tonight) and the live order sheet now use two different sizing
mechanisms.** The section 15 historical NAV numbers (Rs 1,99,862.66 etc.)
validate the coverage-scale design, not tonight's simplified one -- they
should not be read as validating the current live sizing until
`carry_forward_v5.py` is re-run against it, which has not been done.

## Dead code removed

11 one-off root-level `fill_check*.py` debugging scripts (11-12-Aug-2026,
predating the 3-stage mechanism being finalized, zero cross-references
from any production or research code) and their `fill_results*.jsonl`
outputs, `research/fill_realism_v5.py` and `data/fill_realism_v5.jsonl`
(superseded by `fill_realism_v6_3stage.py`/`.jsonl` above),
`data/fill_realism_v5_3stage_backup.jsonl` (its cross-check value is now
captured in the results table above), and `data/carry_forward_v6.json` /
`carry_forward_v6_new.json` (orphaned -- no generating script exists in
the repo anymore). All confirmed zero-reader via repo-wide grep before
deletion, not assumed -- an earlier claim this same night that
`daily_report.build()` was dead code turned out to be false, so this
list was checked, not guessed. Full test suite (59 tests) green after
deletion. `legacy/` (superseded pre-V4 pipeline, already self-documented
as safe to delete) and `research/carry_forward_v5.py`,
`research/book_backfill*.py` (active, referenced above and in
CONTEXT.md) were left untouched.

## `book.json` given the full real Day-1/2/3 fill history for the still-open 28-Jul-2026 cycle

The 4 held names' book records (see item 6 earlier tonight) were
approximated from the expiry-day close, not the real 3-stage mechanism;
the other 6 names had no book record at all. `book.open_position()` now
takes an optional `fill_history` dict, and `entry_tracking.py`'s
`advance()` builds one -- day-by-day proposed price and filled/not --
as it walks the real chain, live, going forward for every future cycle.

For the already-past 28-Jul cycle, `research/
book_backfill_jul28_fillhistory.py` replays the REAL production
functions (`entry_tracking.open_window()` then `advance()` once per real
trading day: 29/30/31-Jul-2026) against the actual cached bhavcopy, not
a reimplementation. Result, transplanted into `data/book.json`:

| Symbol | Day 1 quote | Filled? | Day 2 quote | Filled? | Real entry | Old (stale) cycle_state entry |
|---|---|---|---|---|---|---|
| TRENT, BANDHANBNK, GVT&D, POWERINDIA | market open | yes (Day 1) | -- | -- | = Day-1 open | already matched, unchanged |
| KAYNES | 3314.93 | yes | -- | -- | 3271.00 | already matched, unchanged |
| AMBER | 7406.06 | yes | -- | -- | 7256.00 | already matched, unchanged |
| ADANIGREEN | 1367.44 | yes | -- | -- | **1367.44** | 1383.10 |
| FORCEMOT | 16738.67 | **no** | 17525.18 | yes | **17327.00** | 17375.00 |
| SAIL | 165.30 | **no** | 176.11 | yes | **170.70** | 165.86 |
| IDEA | 12.94 | **no** | 13.17 | yes | **13.05** | 13.02 |

FORCEMOT, SAIL and IDEA actually missed their Day-1 limit and filled on
Day 2 -- `cycle_state.json` had assumed everyone filled at Day-1's open,
the same idealized assumption item 6 above already found and fixed
mechanically; this is that same bug's data, made concrete for real
positions.  `data/cycle_state.json`'s stored `entry` field for these 4
names was corrected to match (stop/target were untouched -- both are
already correctly anchor-based off Day-1's open, which is unaffected by
which day the fill happened).

**ADANIGREEN is the one that matters most: it already exited on
18-Aug-2026, reported to investors as a -5.00% stop-out (computed off
the assumed 1383.10 entry). Its real entry was 1367.44, so the real
exit is -3.91%, not -5.00%.** That number has already gone out. Whether
to communicate a retroactive correction is a decision for a human, not
silently reflected in a future note -- flagged here, not resolved.

## Risk anchor further refined: same-day fills anchor to their own fill price, not Day-1's open (26-Aug-2026)

Working through ADANIGREEN's numbers above surfaced a second, more
fundamental bug in the anchor rule itself, not just its entry price.
`entry_tracking.py` anchored stop/target to Day-1's actual open for
EVERY fill, no matter which day or price it actually filled at. That
rule exists (section 11, "the risk-anchor fix") to stop a DELAYED
Day-2/3 fill from dragging the stop toward a market that moved while
waiting -- real evidence, PNBHOUSING and BSE, both delayed fills. It was
never evidenced for a Day-1 fill, and applying it there anyway broke the
one promise the message makes: "SL: -5%" was silently 5% off a price
never paid whenever a Day-1 limit filled below the open -- exactly what
a limit order under the open is FOR.

ADANIGREEN quoted/filled 1367.44 against a 1383.10 Day-1 open. Anchored
to the open, stop = 1313.945, -3.91% off the real entry, not -5%, and it
triggered on 18-Aug (low 1308.00). Anchored to the actual entry (correct
now): stop = 1299.068 (rounds to 1299.07), a genuine -5%, which
1308.00 does not breach -- the real trigger is 19-Aug, low 1281.10, a
clean stop with no gap (open 1312.90 > stop), same -5.00% loss, one
session later than what was recorded.

## `build_entry_sheet` rewired to read book.json directly instead of re-deriving the outgoing month (26-Aug-2026)

The root cause of the KALYANKJIL/ADANIGREEN mixup two sections up: `build_
entry_sheet` asked `daily_report.build()` to answer "what's currently
held" by re-running selection/ranking against today's data for the
outgoing month -- which answers "what would the algorithm pick if run
again today", not "what was actually held". ASM surveillance is only
ever a current snapshot, so the redo silently un-vetoes a name dropped
a month ago and silently drops a name that was real but has since
changed status. Structural fix, not a patch: `book.holdings_for_expiry()`
(new, merges live book.json + archive by `origin_expiry`) is now the
sole source for "what's currently held" -- no re-derivation, no ASM
dependency, because book.json already has the real answer recorded the
moment it happened. `daily_report.build()` -- the function this
replaces as build_entry_sheet's only caller -- is now dead code,
confirmed by repo-wide grep, not removed yet.

One side effect worth noting, not a new bug: a position already closed
mid-cycle (exit_price set, whether by a real stop/target or because it
was manually archived as part of tonight's catch-up) is deliberately
excluded from the sheet's actionable "SELL — market on open" list --
there's nothing left to sell, and the investor already heard about it
the day it happened. Re-running the sheet against tonight's now-fully-
archived book.json therefore shows an empty sells section for the 6
names closed earlier this session; against the REAL, not-yet-caught-up
state (5 of them still live) it correctly lists all 5 as actionable
SELL orders, verified before and after this fix.

Fixed in `entry_tracking.advance()`, in two passes the same evening.
First pass re-anchored to the actual fill price. Second, cleaner pass
(explicit instruction: "whatever the min basket price was, stop loss
applies to that") -- anchor to the QUOTED min-basket price for whichever
stage actually fills (Day 1's band limit, Day 2's Parkinson re-quote,
Day 3's pooled-vol indicative estimate), not the fill/execution price.
One number now governs both share count AND risk band at every stage;
the two anchors only diverge from each other when a fill gaps through
its own quote (open below a Day-1/2 limit, or Day 3's actual open vs.
its indicative estimate) -- ADANIGREEN filled exactly at its Day-1
quote with no gap, so both passes land on the identical number. The
Day-1-open-anchored gap-abort check ahead of Day 2/3 is unchanged and
deliberately so -- "has this already broken the level we accepted when
we first decided to enter this month" is a Day-1 question by
definition, not a per-stage one. `book_archive.jsonl` and
`cycle_state.json`'s ADANIGREEN record both corrected: anchor 1367.44,
stop 1299.07, exit 1299.07 on 19-Aug (not 1313.94 on 18-Aug). Same
-5.00% loss either way -- only the date and the absolute levels move.

---

# 17. Backtesting formalized: single archive with A/S tags, BACKTEST_VERSION,
#     current version (BT1) fully pre-archived — no cycle needs re-simulating

**26-Aug-2026.** Closed out the three explicit rules negotiated the same
night ("BACKTEST MEANS THE FOLLOWING: 1) basket for the month is the min
basket for new investors 2) simulation only for past months, stored once
so it's never recalculated again 3) all results logged here 4) a new
version only on a stock-picking/entry/exit change") plus the two
corrections that followed (`book_archive.jsonl` stays ONE file with a
kind tag, not two files; live investor reporting via `book.
holdings_for_expiry` never reads a simulated row).

**1. Archive structure.** `book_archive.jsonl` now carries `"kind":
"actual"` or `"kind": "simulated"` on every record, same file, same
schema otherwise. The 6 pre-existing real records (ADANIGREEN, KAYNES,
FORCEMOT, SAIL, IDEA, AMBER) were retrofitted with `kind: "actual"` in
place. `book.holdings_for_expiry` and `book.get_archived` — the only two
readers used for anything investor-facing — now skip any row that is
not `kind == "actual"` (a row with no `kind` at all, i.e. anything
written before tonight, is still treated as actual). `book.
write_simulated_record` and `book.simulated_records` are the write/read
pair for the new kind. Verified: `holdings_for_expiry("2026-07-28")`
still returns exactly the 10 real names, none of the 170 newly-written
simulated rows leaked in.

**2. Versioning.** `config.BACKTEST_VERSION = "BT1"` — a new, explicit
identifier, deliberately NOT reusing "V4" (the live strategy's own name)
or "v5"/"v6" (ad hoc research-script filename iterations), both of
which already meant something else in this codebase and would have been
confusing to overload. Bumps only on one of the three agreed axes:
stock-picking algo, entry strategy, or exit strategy. Tonight's anchor-
rule fix is an entry-strategy change, so archived data starts at BT1,
not BT0. This file stays one continuous document, tagged per section
(as above) — never renamed or split per version.

**3. `research/fill_realism_v6_3stage.py` brought current + archived.**
Its risk-anchor logic still read `anchor = opn1` uniformly for every
fill — stale relative to both of tonight's earlier entry_tracking.py
fixes. Re-pointed to the same rule: `abort_anchor` (Day-1's open) stays
the gap-abort threshold throughout; the anchor actually RECORDED on a
fill is the quoted price for whichever stage fills (`quote1`/`quote2`/
`quote3`). Each symbol's `detail[]` now also carries a full `fill_history`
dict (day1/day2/day3, proposed price + filled flag) identical in shape
to what `book.json` stores for real fills — the "how was this derived"
trail that has to survive without ever re-simulating.

Added `archive_month(y, m)`, which runs the (now-corrected) simulation
once and writes one `kind="simulated"` record per filled symbol via
`book.write_simulated_record` — entry price/date, risk_anchor, the full
fill_history, and exit price/date/reason (read from `res_v6.exits` for
a real STOP/TARGET, or from the month-end open on `roll` for the
carry_forward=False force-close otherwise). Idempotent: a (symbol,
origin_expiry) pair already archived under the current
`BACKTEST_VERSION` is skipped, so re-running never duplicates a line.

Ran `--archive-all` across all 17 cycles studied this session (2025-03
through 2026-07 — the canonical 13-month comparison set plus the 4 live
months since; 2026-08 excluded because it has not concluded, per rule
4). Result: **170 simulated records** (17 cycles × 10 slots), every one
with a non-null `exit_price`, all tagged `backtest_version: "BT1"`.
2026-07's simulated basket differs from that cycle's real (`actual`)
archive by one name (KALYANKJIL vs. ADANIGREEN) — expected and already
documented in `harness.v4_basket`'s own docstring: NSE's ASM feed has no
historical archive, so a backtest basket can never apply the same
surveillance veto a live basket did that month. Not a new finding,
confirmed still true under the fixed anchor logic.

**Net effect:** any future backtest against BT1 for one of these 17
cycles reads straight from `book.simulated_records(backtest_version=
"BT1")` — no bhavcopy load, no classifier call, no re-derivation. A
genuinely new idea (different stop/target rule, different sizing, etc.)
still runs fresh through `research/harness.py` as before; only a
same-version re-ask of "what did this cycle actually do" is now free.

---

# 18. BT1's 13-month total is +33.21%, not +39.20/+39.57% — the anchor
#     fix's real, expected cost, confirmed against the archive

**26-Aug-2026.** Read the freshly-archived BT1 numbers back against the
canonical figures at the top of this file (§0: +39.57% idealized,
+39.20% "real 3-stage" at line 1244) and they do NOT match — a genuine
~6pp gap over the same 13 cycles (2025-03 through 2026-03), not a
transcription error.

Ruled out a bug first: re-ran `research/fill_realism_v6_3stage.py 2025
3` standalone and its own `v6_return_pct` (6.089) matches the archived
records' derived mean (+6.09%) exactly, so the archive write path is
faithful to what `strategy.simulate_month` actually computed. The
fill-day distribution across the 13-cycle archive is 48.5% day-1 /
50.8% day-2 / 0.8% day-3 / 0% aborted — identical to the old "-0.91pt"
research figure at line 1247, so Day-1 sizing / which day a name fills
on did NOT change.

What did change: the risk anchor itself. Of the 67 Day-2/Day-3 fills in
this window, 39 (58%) now hit STOP — because a delayed fill's anchor is
now the QUOTED price for that stage (tonight's fix), which for a name
that missed Day-1 is usually a re-quote ABOVE Day-1's open (the stock
moved away, which is why it missed). Anchoring the stop to that higher
number pulls the stop level up much closer to the actual entry than
the old "always Day-1's open" rule did, so a normal pullback now
triggers a stop that the old, looser anchor would have absorbed.
Sample: KEI (Mar-25) -7.95%, TRENT (Jun-25) -6.20%, several exact -5.00%
prints on the 5% regime-stop cycles — this is the "5% of entry price"
behaviour explicitly asked for two fixes ago, now visible in aggregate.

**This is not a regression to fix — it is the backtested price of the
correction.** The old +39.20/+39.57% totals were generated under a
looser (arguably wrong, per this session's own review) anchor rule that
let late fills run with a stop pinned to a stale, lower reference
price. BT1's +33.21% is the honest number under the anchor rule the
strategy is now actually running live. The two figures are not
interchangeable and should not both be quoted as "the V4 backtest" —
BT1 supersedes them for any future risk/reward discussion, and this
section is the record of why they moved.

Separately, and unrelated to the anchor fix: BT1's Jul-2026 backtest
return (+2.79%, §17) is NOT the same thing as that cycle's real
investor outcome (~+5.3%, recomputed 26-Aug-2026 from the 6 `kind:
"actual"` archive rows plus the 4 still-continuing book.json positions
marked to 25-Aug close) — the backtest basket has no ASM veto and holds
KALYANKJIL where the real basket held ADANIGREEN instead, and the two
names' outcomes differed enough to move the month's average by ~2.5pp.
Backtest and real-money outcomes for the same calendar cycle will keep
diverging by exactly this mechanism for as long as NSE gives no
historical ASM archive to backtest against (documented since
`research/harness.py`'s `v4_basket`).

---

# 19. Complete BT1 backtest, 17 cycles — actual where it exists, simulated
#     only where it doesn't

**26-Aug-2026.** The 10 simulated Jul-28 rows from §17 were deleted —
real `kind: "actual"` data already covers that month in full, so a
simulated shadow of it was pure noise (this is what caused the 2.79%
vs. 5.3% confusion two messages back). `archive_month` now hard-skips
any expiry `book.holdings_for_expiry` already has actual coverage for,
so this can't recur. The table below is the corrected, complete
picture: 16 months read from `book.simulated_records(backtest_version=
"BT1")`, one month (Jul-2026) read from the real archive plus the 4
still-open book.json positions marked to 25-Aug close.

| Cycle | Return | Source |
|---|---|---|
| 2025-03-27 | +6.09% | BT1 sim |
| 2025-04-24 | −4.53% | BT1 sim |
| 2025-05-29 | +1.75% | BT1 sim |
| 2025-06-26 | −1.99% | BT1 sim |
| 2025-07-31 | −3.22% | BT1 sim |
| 2025-08-28 | +4.99% | BT1 sim |
| 2025-09-30 | +0.99% | BT1 sim |
| 2025-10-28 | +0.67% | BT1 sim |
| 2025-11-25 | +2.37% | BT1 sim |
| 2025-12-30 | −2.64% | BT1 sim |
| 2026-01-27 | +11.97% | BT1 sim |
| 2026-02-24 | −5.30% | BT1 sim |
| 2026-03-30 | +22.08% | BT1 sim |
| 2026-04-28 | +0.53% | BT1 sim |
| 2026-05-26 | −2.45% | BT1 sim |
| 2026-06-30 | −1.90% | BT1 sim |
| 2026-07-28 | +5.31% | actual |

**sum +34.71% · mean 2.04%/mo · sd 6.80 · best +22.08% · worst −5.30% ·
positive 10/17.**

This supersedes §17's table (which double-counted nothing, but included
the now-deleted redundant Jul-28 sim row) and the pre-anchor-fix
+39.20%/+39.57% figures at §0/§16 for any live risk/reward discussion
— see §18 for why those don't match.

**This table itself was WRONG and has been superseded again — see §20.**
It was generated before a real archive-derivation bug was caught: don't
trust a backtest table without independently re-running at least one
month fresh and diffing it against the archive, which is exactly what
exposed this one.

---

# 20. §19's table was wrong — re-verified, corrected, and confirmed
#     deterministic this time

**26-Aug-2026, in response to being asked "are we 100% sure."** Not
sugar-coating it: no, §19 was not right, and the honest way to find that
out was to re-run several archived months FRESH and diff them against
what got written, rather than re-asserting the same numbers with more
confidence. That diff immediately failed on 2025-04-24: archived
-4.53%, fresh re-run +3.37%. Two real bugs were under that, found in
sequence, and BOTH are now fixed and independently verified — not just
patched and re-asserted.

**Bug 1 (real, but not the actual cause of the -4.53/+3.37 gap):**
`res_baseline`/`res_v6` inside `fill_realism_v6_3stage.py` never passed
`use_classifier`, so both defaulted through `config.
CORP_ACTION_GREY_ZONE_ENABLED=True` into the live, network/LLM-backed
corporate-action classifier — which this offline sandbox cannot reach
deterministically. Fixed: `use_classifier=False` on the archived
`simulate_month` call, and `config.CORP_ACTION_LLM_ENABLED = False` set
at module scope in the backtest script only (config.py's persisted
value, which the live report reads, is untouched). This is a correct,
worthwhile fix — strategy.py's own docstring says backtests must be
deterministic and offline — but re-running after this fix STILL gave a
different number for 2025-04-24 (-4.53% vs +2.81%), proving it wasn't
the real cause of that specific gap.

**Bug 2 (the actual cause).** `strategy.simulate_month` calls
`adjust_holding_window` INTERNALLY and walks its own corp-action-
adjusted copy of the price series for every stop/target/pnl decision —
that adjusted copy is what `v6_return_pct` reflects. It does not mutate
or hand back the caller's own price dict, which stays raw. The
archiving script's ROLLOVER-exit fallback read the exit price straight
from that raw, caller-side dict — so BSE's real 23-May-2025 2:1 split
showed as an unadjusted close of 2472.50 (a fake -60.48% "loss" against
its 6257.07 entry) while `simulate_month`'s own internal walk correctly
saw ~7066.52 (factor 2.858, real result closer to +13%). Fixed: the
script now independently calls `strategy.adjust_holding_window` with
the exact same arguments `simulate_month` uses internally, and reads
the ROLLOVER exit price off that adjusted series instead of raw
bhavcopy.

**Verification, not re-assertion.** Wiped all 160 (later 130, after the
first partial fix) simulated rows twice and rebuilt from scratch each
time. After the real fix, wrote `research/_verify_bt1.py`: re-runs
every one of the 16 simulated months completely fresh and checks three
things against the archive independently — (1) `v6_return_pct` (the
number `simulate_month` itself produces) matches the archive-derived
mean to within rounding, (2) `n_aborted == 0` for every month (so
"mean of 10" and "mean of the archived rows" are the same divisor —
an aborted slot would silently shrink the archive without simulate_
month's own convention changing), (3) every archived `risk_anchor`
matches the exact stage-quote it should (quote1/quote2/quote3, per the
25/26-Aug anchor fix). Result: **17 months checked (16 sim, one repeat
across batches), 0 mismatches.** `python3 -m pytest tests/` still 59/59.

**Corrected, verified 17-cycle table** (replaces §19's table in full):

| Cycle | Return | Source |
|---|---|---|
| 2025-03-27 | +6.09% | BT1 sim |
| 2025-04-24 | +2.81% | BT1 sim |
| 2025-05-29 | +1.75% | BT1 sim |
| 2025-06-26 | −1.99% | BT1 sim |
| 2025-07-31 | −3.78% | BT1 sim |
| 2025-08-28 | +4.68% | BT1 sim |
| 2025-09-30 | +3.46% | BT1 sim |
| 2025-10-28 | +0.67% | BT1 sim |
| 2025-11-25 | +2.37% | BT1 sim |
| 2025-12-30 | −2.64% | BT1 sim |
| 2026-01-27 | +11.97% | BT1 sim |
| 2026-02-24 | −5.30% | BT1 sim |
| 2026-03-30 | +22.08% | BT1 sim |
| 2026-04-28 | +0.53% | BT1 sim |
| 2026-05-26 | −2.45% | BT1 sim |
| 2026-06-30 | −1.57% | BT1 sim |
| 2026-07-28 | +5.31% | actual |

**sum +44.00% · mean 2.59%/mo · sd 6.59 · best +22.08% · worst −5.30% ·
positive 11/17.**

Only three months changed from §19 (2025-04, 2025-07, 2025-09, 2026-06
— wherever an unadjusted corporate action happened to land on the
rollover date); the rest were already correct because no split fell on
those specific exit dates. That is exactly why a spot-check on one or
two "reasonable-looking" months would not have caught this — the bug
only fires when a real corporate action lands on a ROLLOVER exit day,
which is a minority of rows, and every other row looks completely
normal. `research/_verify_bt1.py` is kept as a permanent regression
check: re-run it after any future change to `fill_realism_v6_3stage.py`
or to `strategy.adjust_holding_window`/`simulate_month`, before trusting
a new archive.
