"""
Overlap-maximising selector: match Altcase's monthly ten, Feb-Jul 2026.

    python tools_overlap.py build     # cache features for each month
    python tools_overlap.py score     # rank variants by hit rate

Scored on OVERLAP, not returns. The target is 6/10.

WHY THE CURRENT ENGINE MISSES
  V4 ranks on 0.50*z(volatility) + 0.30*z(rollover) + 0.20*z(carry) --
  derivatives data. Their weekly report shows they rank on price
  structure (a discrete Strong/Mild Bullish trend state), sector
  leadership (the W-o-W sector heatmap, leaders first) and a volume
  surge scan. Near-orthogonal inputs, hence ~7% historical overlap.

FEATURES BUILT HERE, mirroring their stated process
  trend      close vs 20/50/100/200 DMA, stacked in order = "strong"
  rs         20D and 60D return ranks
  sector     sector median 20D return, ranked leaders first
  volume     5D turnover vs 20D turnover (their surge scan)
  proximity  distance below the 52w high (they buy near highs)

Selection date is the last session BEFORE the 1st of the month, which is
when their basket is issued -- not our expiry date.

CEILING: their pool is NIFTY 500 and ~16 of 38 weekly shortlisted names
are cash-only. Any name outside the F&O universe is unreachable for us,
so the achievable hit rate is capped below 10/10. Reported per month.

State lives in /tmp/overlap.json.
"""
import datetime as dt
import json
import logging
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

logging.disable(logging.CRITICAL)
import strategy                                             # noqa: E402

STATE = "/tmp/overlap.json"
MONTHS = ["2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09",
          "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
          "2026-04", "2026-07"]
# fit on everything up to Dec-2025, hold out 2026 as a real test
TRAIN = MONTHS[:9]
HOLDOUT = MONTHS[9:]
SLOTS = 10
SECTOR_CAP = 3

ALIAS = {
    "FEDERALBK": "FEDERALBNK", "NALCO": "NATIONALUM", "JSPL": "JINDALSTEL",
    "AVENUE SUPERMARTS": "DMART", "PREMIER ENERGIES": "PREMIERENE",
    "WAAREE ENERGIES": "WAAREEENER", "NESTLE": "NESTLEIND",
    "SOLARINDS": "SOLARINDS", "GMRAIRPORT": "GMRAIRPORT", "BANDHANBNK": "BANDHANBNK",
    "UNIONBANK": "UNIONBANK", "POWERGRID": "POWERGRID", "MARICO": "MARICO",
}


def to_symbol(name, uni):
    n = name.strip().upper()
    if n in ALIAS and ALIAS[n] in uni:
        return ALIAS[n]
    if n in uni:
        return n
    c = [u for u in uni if u.startswith(n[:6])]
    return c[0] if len(c) == 1 else None


def sel_date(key, alldates):
    y, m = map(int, key.split("-"))
    first = dt.date(y, m, 1)
    prior = [d for d in alldates if d < first]
    return prior[-1] if prior else None


def features(hist, uni, d, sec):
    alld = [x for x in sorted(hist) if x <= d]
    rows = []
    for s in uni:
        cl, tv, hi = [], [], []
        for x in alld[-260:]:
            f = hist[x]
            if s not in f.index:
                continue
            c = f.at[s, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            cl.append(float(c))
            hi.append(float(f.at[s, "high_price"]) if pd.notna(f.at[s, "high_price"]) else float(c))
            t = f.at[s, "turnover"] if "turnover" in f.columns else np.nan
            tv.append(float(t) if pd.notna(t) else np.nan)
        if len(cl) < 210:
            continue
        ser = pd.Series(cl)
        c = cl[-1]
        d20, d50 = ser.tail(20).mean(), ser.tail(50).mean()
        d100, d200 = ser.tail(100).mean(), ser.tail(200).mean()
        stack = int(c > d20) + int(d20 > d50) + int(d50 > d100) + int(d100 > d200)
        t5 = np.nanmean(tv[-5:]) if len(tv) >= 5 else np.nan
        t20 = np.nanmean(tv[-20:]) if len(tv) >= 20 else np.nan
        rows.append(dict(
            symbol=s, sector=sec.get(s, "Unclassified"), close=c,
            above20=int(c > d20), above50=int(c > d50), above200=int(c > d200),
            stack=stack,
            r20=c / cl[-21] - 1, r60=c / cl[-61] - 1,
            volsurge=(t5 / t20) if t5 and t20 and t20 > 0 else np.nan,
            turnover=t20,
            from52wh=c / max(hi) - 1))
    return pd.DataFrame(rows)


def z(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std()
    return (s - s.mean()) / sd if sd and sd > 0 else s * 0.0


def pick(df, w, min_turnover=5e7, regime=False):
    """
    `regime=True` flips the selector with the market state.

    Their published baskets show one strategy with two faces: in a healthy
    tape they buy leaders at their highs (Jul-26: all ten above the 20 and
    50 DMA, 1.4% off the 52w high); after a crash they buy the wreckage
    (Apr-26: eight of ten BELOW both DMAs, 22.5% off the high, in a
    universe whose median 20D return was -12.58%).

    So when the universe median 20D return is negative, the DMA gate is
    dropped and the RS and 52w-high terms invert -- rank the most beaten
    first instead of the strongest.
    """
    d = df.copy()
    d = d[d["turnover"] > min_turnover]
    if d.empty:
        return []
    risk_on = (not regime) or (d["r20"].median() > 0)
    if risk_on:
        d = d[(d["above20"] == 1) & (d["above50"] == 1)]
    sign = 1.0 if risk_on else -1.0
    if d.empty:
        return []
    sm = d.groupby("sector")["r20"].median().rank(ascending=False)
    d["secrank"] = -d["sector"].map(sm).fillna(sm.max())
    d["score"] = (sign * w["trend"] * z(d["stack"])
                  + sign * w["rs20"] * z(d["r20"])
                  + sign * w["rs60"] * z(d["r60"])
                  + w["sector"] * z(d["secrank"])
                  + w["vol"] * z(d["volsurge"].fillna(1.0))
                  + sign * w["near"] * z(d["from52wh"]))
    d = d.sort_values("score", ascending=False)
    out, cnt = [], {}
    for _, r in d.iterrows():
        s = r["sector"]
        if cnt.get(s, 0) >= SECTOR_CAP:
            continue
        out.append(r["symbol"])
        cnt[s] = cnt.get(s, 0) + 1
        if len(out) >= SLOTS:
            break
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    alt = json.load(open("/tmp/alt_all.json"))
    uset = set(uni)

    if cmd == "build":
        st = json.load(open(STATE)) if os.path.exists(STATE) else {"done": []}
        todo = [k for k in MONTHS if k not in st["done"]]
        if not todo:
            print("all built -- run `score`")
            return
        k = todo[0]
        y, m = map(int, k.split("-"))
        anchor = dt.date(y, m, 15)
        hist = strategy.load_price_history(anchor, uni, days=400)
        d = sel_date(k, sorted(hist))
        f = features(hist, uni, d, sec)
        f.to_json(f"/tmp/ovfeat_{k}.json", orient="split", index=False)
        st["done"].append(k)
        json.dump(st, open(STATE, "w"))
        tgt = [to_symbol(x, uset) for x in alt[k]]
        miss = [x for x, t in zip(alt[k], tgt) if t is None]
        print(f"{k}  sel date {d}  rows {len(f)}  "
              f"their names in F&O {sum(1 for t in tgt if t)}/10  unreachable {miss}"
              f"   [{len(st['done'])}/{len(MONTHS)}]")
        return

    # ---- score ----
    grids = {
        "trend": [0, 1, 2], "rs20": [0, 1, 2], "rs60": [0, 1, 2],
        "sector": [0, 1, 2], "vol": [0, 1], "near": [0, 1, 2],
    }
    feats = {k: pd.read_json(f"/tmp/ovfeat_{k}.json", orient="split") for k in MONTHS}
    targets = {k: set(x for x in (to_symbol(n, uset) for n in alt[k]) if x) for k in MONTHS}
    ceiling = {k: len(targets[k] & set(feats[k]["symbol"])) for k in MONTHS}
    print("reachable ceiling per month:",
          {k: f"{v}/10" for k, v in ceiling.items()},
          f" total {sum(ceiling.values())}/{10 * len(MONTHS)}")

    keys = list(grids)
    rows = []
    for combo in product(*[grids[k] for k in keys]):
        w = dict(zip(keys, combo))
        if sum(combo) == 0:
            continue
        per = {k: len(set(pick(feats[k], w)) & targets[k]) for k in MONTHS}
        rows.append((sum(per[k] for k in TRAIN), sum(per[k] for k in HOLDOUT), w, per))

    # pick the winner on TRAIN only, then read its HOLDOUT score
    rows.sort(key=lambda x: -x[0])
    tr_max, ho_of_best, wbest, per_best = rows[0]
    print(f"\nfit on {len(TRAIN)} train months, tested on {len(HOLDOUT)} holdout months")
    print(f"  best TRAIN weights: " + "  ".join(f"{k}={v}" for k, v in wbest.items()))
    print(f"  TRAIN   {tr_max}/{10*len(TRAIN)}  ({tr_max/len(TRAIN):.2f}/10 per month)")
    print(f"  HOLDOUT {ho_of_best}/{10*len(HOLDOUT)}  ({ho_of_best/len(HOLDOUT):.2f}/10 per month)  <-- the honest number")
    print(f"  per month: {per_best}")

    # how much of the train score was luck? compare to the best possible holdout
    best_ho = max(r[1] for r in rows)
    print(f"\n  best HOLDOUT achievable by ANY weight set: {best_ho}/{10*len(HOLDOUT)} "
          f"({best_ho/len(HOLDOUT):.2f}/10) -- unattainable in advance, shows the ceiling")
    print(f"\ntop 5 by TRAIN, with their holdout:")
    for tr, ho, w, per in rows[:5]:
        print(f"  train {tr:>2}/{10*len(TRAIN)}  holdout {ho:>2}/{10*len(HOLDOUT)}   "
              + "  ".join(f"{k}={v}" for k, v in w.items()))
    print("\nfor reference, V4's historical overlap was ~0.7/10 per month")


if __name__ == "__main__":
    main()
