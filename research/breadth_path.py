"""
Does breadth DETERIORATION during a cycle predict the rest of that cycle?

    python research/breadth_path.py          # advance one cycle
    python research/breadth_path.py report

Different question from the weekly de-risk trigger already rejected. That
asked "should we ACT on falling breadth". This asks the prior question:
does breadth falling during a cycle carry any information about the
return still to come? If it does not, no trigger built on it can work,
whatever the thresholds.

For every session in every holding window it records breadth (% of the
F&O universe above its own 200 DMA) alongside the book's return so far
and the return still to come, so the two can be correlated directly.

Loading 560 sessions at once exceeds the shell timeout, so each cycle
loads its own 240-session window and the results accumulate.

State in /tmp/breadth_path.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys

import numpy as np
import pandas as pd

import harness                                              # noqa: E402
import strategy                                             # noqa: E402

STATE = "/tmp/breadth_path.json"
DOD = "/tmp/dod.json"


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"done": [], "rows": []}


def breadth_over(window_end, uni, need_days):
    """{date: breadth%} for the sessions needed, using a 240-day load."""
    hist = strategy.load_price_history(window_end, uni, days=240)
    alld = sorted(hist)
    cols = {}
    for s in uni:
        v = []
        for d in alld:
            f = hist[d]
            x = np.nan
            if s in f.index:
                c = f.at[s, "close_price"]
                if pd.notna(c) and c > 0:
                    x = float(c)
            v.append(x)
        cols[s] = v
    px = pd.DataFrame(cols, index=alld)
    dma = px.rolling(200, min_periods=200).mean()
    above, valid = (px > dma), (dma.notna() & px.notna())
    n = valid.sum(axis=1)
    # the first 199 sessions have no 200-DMA, so n is 0 there
    br = (above.where(valid).sum(axis=1) / n.replace(0, np.nan) * 100)
    return {d: float(br.loc[d]) for d in need_days
            if d in br.index and pd.notna(br.loc[d])}


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'cycle':<9}{'entry br':>9}{'min br':>8}{'end br':>8}"
              f"{'drop':>7}{'final':>8}")
        for r in rows:
            print(f"{r['k']:<9}{r['entry_br']:>9.1f}{r['min_br']:>8.1f}"
                  f"{r['end_br']:>8.1f}{r['entry_br']-r['min_br']:>7.1f}"
                  f"{r['final']:>7.2f}%")

        obs = [o for r in rows for o in r["obs"] if o["runway"] >= 3]
        if len(obs) < 10:
            return
        # Does breadth change so far predict the return still to come?
        bc = [o["br"] - o["entry_br"] for o in obs]
        fw = [o["fwd"] for o in obs]
        n = len(obs)
        mb, mf = st.mean(bc), st.mean(fw)
        cov = sum((bc[i] - mb) * (fw[i] - mf) for i in range(n)) / n
        r = cov / (st.pstdev(bc) * st.pstdev(fw)) if st.pstdev(bc) else 0.0
        print(f"\n{n} sessions with >=3 left")
        print(f"corr(breadth change since entry, return still to come) = {r:+.3f}")

        print("\nreturn still to come, bucketed by how far breadth has fallen:")
        for lo, hi, lbl in [(-100, -15, "fell >15pp"), (-15, -10, "fell 10-15"),
                            (-10, -5, "fell 5-10"), (-5, 0, "fell 0-5"),
                            (0, 100, "flat or up")]:
            b = [o["fwd"] for o in obs
                 if lo <= (o["br"] - o["entry_br"]) < hi]
            if not b:
                continue
            print(f"  {lbl:<12} n={len(b):>4}   mean {st.mean(b):>+6.2f}%   "
                  f"median {st.median(b):>+6.2f}%   "
                  f"positive {sum(1 for x in b if x > 0)}/{len(b)}")
        return

    src = json.load(open(DOD))["rows"]
    todo = [r for r in src if r["k"] not in s["done"]]
    if not todo:
        print("all built -- run `report`")
        return
    row = todo[0]
    y, m = map(int, row["k"].split("-"))
    ex, nx = harness.cycle_dates(y, m)
    uni = harness.universe()
    curve = [(pd.to_datetime(d).date(), v) for d, v in row["curve"]]
    days = [d for d, _v in curve]
    br = breadth_over(days[-1], uni, [ex] + days)

    entry_br = br.get(ex)
    if entry_br is None:
        entry_br = br.get(days[0], float("nan"))
    obs = []
    for i, (d, v) in enumerate(curve):
        if d not in br:
            continue
        obs.append({"date": str(d), "br": br[d], "entry_br": entry_br,
                    "sofar": v, "fwd": curve[-1][1] - v,
                    "runway": len(curve) - 1 - i})
    vals = [o["br"] for o in obs]
    s["rows"].append({"k": row["k"], "stop": row["stop"], "final": row["final"],
                      "entry_br": entry_br, "min_br": min(vals) if vals else float("nan"),
                      "end_br": vals[-1] if vals else float("nan"), "obs": obs})
    s["done"].append(row["k"])
    json.dump(s, open(STATE, "w"))
    print(f"{row['k']}  entry breadth {entry_br:.1f}%  min {min(vals):.1f}%  "
          f"end {vals[-1]:.1f}%  final {row['final']:+.2f}%   "
          f"[{len(s['done'])}/{len(src)}]")


if __name__ == "__main__":
    main()
