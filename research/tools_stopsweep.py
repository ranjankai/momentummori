"""
Sweep the stop width across all 13 cycles on the LIVE V4 basket.

    python tools_stopsweep.py          # advance one cycle
    python tools_stopsweep.py report   # print the table

Fresh-start and additive throughout: Rs100 in at the first session after
expiry, fully out at the first session after the next expiry, monthly
returns summed.

The question this answers: in the five losing months, is the 5% stop
protecting the book or shaking it out? "none" removes the stop entirely
and holds every name to rollover, so the difference between a stop level
and "none" is exactly what that stop cost or saved.

The 40% target is left in place for every arm so only the stop varies.

State lives in /tmp/stopsweep.json.
"""
import datetime as dt
import json
import logging
import os
import statistics as st
import sys

import pandas as pd

logging.disable(logging.CRITICAL)
import nse_client                                           # noqa: E402
import scoring                                              # noqa: E402
import strategy                                             # noqa: E402
from tools_matrix import bars                               # noqa: E402

STATE = "/tmp/stopsweep.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]
SLOTS = 10
TARGET_PCT = 40.0
ARMS = [3.0, 5.0, 7.0, 10.0, 15.0, None]      # None = no stop at all


def label(a):
    return "none" if a is None else f"{a:.0f}%"


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def walk(seq, entry, stop_pct):
    """
    Resting-order execution, which is how this is actually traded: the SL
    and the target are live orders sitting at the exchange from the first
    morning, so they fill AT their price the instant it trades -- not at
    the next open.

    The one exception is a gap: if the session OPENS through the level,
    the resting order fills at the open, which is worse than the stop and
    better than the target. That is the only way a stop can lose more than
    its width.
    """
    target = entry * (1 + TARGET_PCT / 100)
    stop = entry * (1 - stop_pct / 100) if stop_pct is not None else None
    for d, o, h, l, c in seq:
        if stop is not None and l <= stop:
            return (min(o, stop), "STOP")          # gap-down fills at open
        if h >= target:
            return (max(o, target), "TARGET")      # gap-up fills at open
    return seq[-1][1], "ROLLOVER"       # rollover exits at the roll-day OPEN


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        keys = [label(a) for a in ARMS]
        print(f"{'cycle':<9}{'breadth':>8}{'live':>6}"
              + "".join(f"{k:>9}" for k in keys))
        for r in rows:
            print(f"{r['k']:<9}{r['breadth']:>8.1f}{r['live']:>5.0f}%"
                  + "".join(f"{r['arms'][k]:>8.2f}%" for k in keys))
        print()
        for k in keys:
            x = [r["arms"][k] for r in rows]
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5)
            print(f"stop {k:<5} sum {sum(x):>+7.2f}%   mean {st.mean(x):>5.2f}%   "
                  f"sd {st.stdev(x):>5.2f}   worst {min(x):>6.2f}%   "
                  f"pos {sum(1 for v in x if v > 0):>2}/{len(x)}   t {t:>5.2f}")
        weak = [r for r in rows if r["arms"]["5%"] < 0]
        if weak:
            print(f"\n--- the {len(weak)} months that lost under the 5% stop ---")
            print(f"{'cycle':<9}" + "".join(f"{k:>9}" for k in keys))
            for r in weak:
                print(f"{r['k']:<9}" + "".join(f"{r['arms'][k]:>8.2f}%" for k in keys))
            for k in keys:
                x = [r["arms"][k] for r in weak]
                print(f"  stop {k:<5} sum {sum(x):>+7.2f}%")
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
    breadth = strategy.market_breadth(ex, uni, hist)
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

    seqs = {}
    for sym in picks:
        q = bars(merged, sym, ex, roll)
        if len(q) >= 2:
            seqs[sym] = q

    arms = {}
    for a in ARMS:
        tot = 0.0
        for sym, q in seqs.items():
            entry = q[0][1]
            px, _ = walk(q[1:], entry, a)
            tot += (px / entry - 1) * 100
        arms[label(a)] = tot / SLOTS

    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": breadth, "live": live,
                      "arms": arms, "n": len(seqs)})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{y}-{m:02d}  breadth {breadth:.1f}%  live {live:.0f}%  | "
          + "  ".join(f"{k} {v:+.2f}%" for k, v in arms.items())
          + f"   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
