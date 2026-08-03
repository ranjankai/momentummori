"""
Weekly breadth de-risk overlay on the live V4 basket, 13 cycles.

    python tools_derisk.py          # advance one cycle
    python tools_derisk.py report   # print the sweep

Position sizing is untouched: always 10 names at 10%. The overlay may
only TIGHTEN stops or flatten to cash -- never resize, never loosen.

Breadth is re-read every 5th trading session. Two trigger shapes:

  ABS   fire when breadth < floor
  DROP  fire when breadth has fallen `floor` points BELOW the entry-day
        reading

ABS is expected to fail: Jan-26 and Mar-26 open at breadth 40.0% and
19.4% -- the two best cycles in the sample start in the state an absolute
floor calls dangerous. DROP conditions on deterioration instead of level.

Actions on fire: tighten every live stop to N%, or exit everything at the
next open. Once fired, the cycle never reverts.

Baseline arm is the live regime config, so every number is directly
comparable to tools_stopsweep's live column (+39.57%).

State lives in /tmp/derisk.json.
"""
import datetime as dt
import json
import logging
import os
import statistics as st
import sys

import numpy as np
import pandas as pd

logging.disable(logging.CRITICAL)
import nse_client                                           # noqa: E402
import scoring                                              # noqa: E402
import strategy                                             # noqa: E402
from tools_matrix import bars                               # noqa: E402

STATE = "/tmp/derisk.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]
SLOTS = 10
TARGET_PCT = 40.0
CHECK_EVERY = 5                       # trading sessions between breadth reads

# (shape, threshold, action) -- action "cash" or a tightened stop in %
ARMS = [
    ("base", None, None),
    ("ABS", 40.0, "cash"), ("ABS", 40.0, 3.0),
    ("ABS", 35.0, "cash"), ("ABS", 35.0, 3.0),
    ("DROP", 5.0, "cash"), ("DROP", 5.0, 3.0),
    ("DROP", 10.0, "cash"), ("DROP", 10.0, 3.0),
    ("DROP", 15.0, "cash"), ("DROP", 15.0, 3.0),
]


def arm_key(a):
    shape, thr, act = a
    if shape == "base":
        return "base"
    return f"{shape}{thr:.0f}-{'cash' if act == 'cash' else f'{act:.0f}%'}"


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def breadth_series(hist, uni, dates):
    """% of the universe above its own 200-DMA, for each date in `dates`."""
    alld = sorted(hist)
    cols = {}
    for s in uni:
        vals = []
        for d in alld:
            f = hist[d]
            v = np.nan
            if s in f.index:
                c = f.at[s, "close_price"]
                if pd.notna(c) and c > 0:
                    v = float(c)
            vals.append(v)
        cols[s] = vals
    px = pd.DataFrame(cols, index=alld)
    dma = px.rolling(200, min_periods=200).mean()
    above = (px > dma)
    valid = dma.notna() & px.notna()
    out = {}
    for d in dates:
        if d not in px.index:
            continue
        n = int(valid.loc[d].sum())
        out[d] = (float(above.loc[d][valid.loc[d]].sum()) / n * 100) if n else float("nan")
    return out


def walk(seq, entry, stop_pct, fire_at=None, action=None):
    """
    Resting-order fills. `fire_at` is the session index from which the
    de-risk action applies.
    """
    target = entry * (1 + TARGET_PCT / 100)
    stop = entry * (1 - stop_pct / 100)
    for i, (d, o, h, l, c) in enumerate(seq):
        if fire_at is not None and i == fire_at:
            if action == "cash":
                return o, "DERISK-CASH"
            tight = entry * (1 - action / 100)
            stop = max(stop, tight)          # never widen
        if l <= stop:
            return min(o, stop), "STOP"
        if h >= target:
            return max(o, target), "TARGET"
    return seq[-1][1], "ROLLOVER"


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        keys = [arm_key(a) for a in ARMS]
        weak = {"2025-06", "2025-07", "2025-12", "2026-02"}
        best = {"2026-01", "2026-03"}
        print(f"{'arm':<16}{'sum':>9}{'weak4':>9}{'best2':>9}{'worst':>8}"
              f"{'sd':>7}{'pos':>6}{'fires':>7}")
        for k in keys:
            x = [r["arms"][k] for r in rows]
            w = sum(r["arms"][k] for r in rows if r["k"] in weak)
            b = sum(r["arms"][k] for r in rows if r["k"] in best)
            f = sum(1 for r in rows if r["fired"].get(k))
            print(f"{k:<16}{sum(x):>+8.2f}%{w:>8.2f}%{b:>8.2f}%{min(x):>7.2f}%"
                  f"{st.stdev(x):>7.2f}{sum(1 for v in x if v > 0):>4}/13{f:>7}")
        print("\nper-cycle, base vs the best DROP arm:")
        print(f"{'cycle':<9}{'breadth':>8}{'min wk':>8}{'base':>9}"
              + "".join(f"{k:>13}" for k in ("DROP10-cash", "DROP10-3%")))
        for r in rows:
            print(f"{r['k']:<9}{r['breadth']:>8.1f}{r['minwk']:>8.1f}"
                  f"{r['arms']['base']:>8.2f}%"
                  + "".join(f"{r['arms'][k]:>12.2f}%" for k in ("DROP10-cash", "DROP10-3%")))
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    y, m = MONTHS[i]
    td = strategy.known_trading_days()
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    ex = strategy.expiry_for(y, m, trading_days=td)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nx = strategy.expiry_for(ny, nm, trading_days=td)

    hist = strategy.load_price_history(ex, uni)
    live = strategy.resolve_stop_pct(ex, uni, hist)
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, _ = strategy.rank_universe(sig, sec)
    picks = basket["symbol"].tolist()[:SLOTS]

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12), uni, days=20))
    after = [d for d in sorted(merged) if d > nx]
    roll = after[0] if after else nx

    hold = [d for d in sorted(merged) if ex < d <= roll]
    checks = [hold[j] for j in range(CHECK_EVERY - 1, len(hold), CHECK_EVERY)]
    bs = breadth_series(merged, uni, [ex] + checks)
    b0 = bs.get(ex, float("nan"))
    wk = [bs[d] for d in checks if d in bs and not np.isnan(bs[d])]

    seqs = {}
    for sym in picks:
        q = bars(merged, sym, ex, roll)
        if len(q) >= 2:
            seqs[sym] = q

    arms, fired = {}, {}
    for a in ARMS:
        shape, thr, act = a
        k = arm_key(a)
        idx = None
        if shape != "base":
            for j, d in enumerate(checks):
                bv = bs.get(d)
                if bv is None or np.isnan(bv):
                    continue
                hit = (bv < thr) if shape == "ABS" else ((b0 - bv) >= thr)
                if hit:
                    idx = hold.index(d)
                    break
        fired[k] = idx is not None
        tot = 0.0
        for sym, q in seqs.items():
            entry = q[0][1]
            # seq passed to walk starts one session after entry
            fa = (idx - 1) if idx is not None and idx >= 1 else None
            px, _ = walk(q[1:], entry, live, fire_at=fa, action=act)
            tot += (px / entry - 1) * 100
        arms[k] = tot / SLOTS

    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": b0, "live": live,
                      "minwk": min(wk) if wk else float("nan"),
                      "arms": arms, "fired": fired})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{y}-{m:02d}  b0 {b0:.1f}%  min weekly {min(wk) if wk else float('nan'):.1f}%  "
          f"base {arms['base']:+.2f}%  DROP10-cash {arms['DROP10-cash']:+.2f}%  "
          f"fires {sum(fired.values())}/{len(fired)}   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
