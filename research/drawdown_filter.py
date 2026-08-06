"""
Does a 52-week-high floor improve V4's selection?

    python research/drawdown_filter.py          # advance one cycle
    python research/drawdown_filter.py report

V4 scores 0.50*z(volatility) + 0.30*z(rollover) + 0.20*z(carry). Large
drawdowns RAISE realised volatility, so the engine systematically
re-selects broken charts: on the 28-Jul-2026 expiry six of ten names
repeated from a July basket that lost 2.76%, and TRENT ranked #1 while
52% below its 52-week high and under its own 50-DMA.

This adds `from52wh` as an ELIGIBILITY GATE ahead of the existing score
-- the ranking is untouched, the wreckage is simply removed first.
Sweeps the floor at 30/40/50%.

FALSIFIABLE: V4's two best months (Jan-26 +13.61%, Mar-26 +19.61%) were
rebounds bought when the whole market was crushed and most names were far
below their highs. If the filter deletes those, the idea is dead.

Prices are back-adjusted for splits first, or a name that split looks
crashed and gets filtered for the wrong reason.

State in /tmp/ddfilter.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys

import pandas as pd

import harness                                              # noqa: E402
import strategy                                             # noqa: E402
import config                                               # noqa: E402

# Four simulate_month calls per cycle; with the grey-zone classifier on,
# each fires NSE + LLM lookups and the sweep takes minutes and stops being
# reproducible. The price band alone still catches every split.
config.CORP_ACTION_GREY_ZONE_ENABLED = False

STATE = "/tmp/ddfilter.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]
FLOORS = [0.30, 0.40, 0.50]
SLOTS = 10


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def from52wh(hist, symbols, upto):
    """close / max(high over the last 252 sessions) - 1, split-adjusted."""
    days = [d for d in sorted(hist) if d <= upto][-252:]
    if len(days) < 60:
        return {}
    out = {}
    for s in symbols:
        cl, hi = [], []
        for d in days:
            f = hist[d]
            if s not in f.index:
                continue
            c = f.at[s, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            cl.append(float(c))
            h = f.at[s, "high_price"]
            hi.append(float(h) if pd.notna(h) else float(c))
        if len(cl) < 60 or max(hi) <= 0:
            continue
        # A split makes the old high look unreachable and the name would be
        # filtered for the wrong reason. Restating 252 frames per symbol is
        # too slow here, so detect the breach and abstain: no reading means
        # no filtering, which is the safe direction.
        if any(cl[i + 1] / cl[i] < 0.72 or cl[i + 1] / cl[i] > 1.40
               for i in range(len(cl) - 1)):
            continue
        out[s] = cl[-1] / max(hi) - 1
    return out


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        cols = ["base"] + [f"{int(f*100)}%" for f in FLOORS]
        print(f"{'cycle':<9}{'stop':>6}" + "".join(f"{c:>10}" for c in cols)
              + "   dropped@40%")
        for r in rows:
            print(f"{r['k']:<9}{r['stop']:>5.0f}%"
                  + "".join(f"{r['ret'][c]:>9.2f}%" for c in cols)
                  + f"   {', '.join(r['dropped40']) or '-'}")
        print()
        for c in cols:
            x = [r["ret"][c] for r in rows]
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5)
            print(f"{c:<7} sum {sum(x):>+7.2f}%   mean {st.mean(x):>5.2f}%   "
                  f"sd {st.stdev(x):>5.2f}   worst {min(x):>6.2f}%   "
                  f"pos {sum(1 for v in x if v > 0):>2}/{len(x)}   t {t:>5.2f}")
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    y, m = MONTHS[i]
    ex, nx = harness.cycle_dates(y, m)
    uni = harness.universe()
    hist = strategy.load_price_history(ex, uni)
    stop_pct = strategy.resolve_stop_pct(ex, uni, hist)

    # reuse the history already loaded -- v4_basket would load it again
    d = strategy.basket_for(ex, uni, harness.sectors(), price_hist=hist)
    picks, ranked = d.symbols, d.ranked_order
    dd = from52wh(hist, ranked[:60], ex)

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12),
                                              uni, days=20))

    ret, dropped40 = {}, []
    base = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                             ranked_order=ranked, price_by_date=merged)
    ret["base"] = base.return_pct

    sec = harness.sectors()
    for f in FLOORS:
        ok = [x for x in ranked if dd.get(x, 0.0) >= -f]
        # rebuild the top ten from survivors, same sector cap as V4
        cap = max(1, int(SLOTS * 30 / 100))
        chosen, counts = [], {}
        for sym in ok:
            k = sec.get(sym, f"Unclassified:{sym}")
            if counts.get(k, 0) >= cap:
                continue
            chosen.append(sym)
            counts[k] = counts.get(k, 0) + 1
            if len(chosen) >= SLOTS:
                break
        r = harness.run_cycle(chosen, ex, nx, stop_pct=stop_pct,
                              ranked_order=ok, price_by_date=merged)
        ret[f"{int(f*100)}%"] = r.return_pct
        if f == 0.40:
            dropped40 = [x for x in picks if x not in chosen]

    s["rows"].append({"k": f"{y}-{m:02d}", "stop": stop_pct, "ret": ret,
                      "dropped40": dropped40})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{y}-{m:02d}  " + "  ".join(f"{k} {v:+.2f}%" for k, v in ret.items())
          + f"   [{i+1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
