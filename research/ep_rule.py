"""
Two questions about a drop-from-entry exit rule.

    python research/ep_rule.py stock      # per-STOCK EP analysis
    python research/ep_rule.py portfolio  # regime-conditional portfolio rule

Reuses the daily curves and baskets already computed by dod_threshold.py
(/tmp/dod.json), plus per-stock closes rebuilt from cache.

Q1  Per stock: is there a drop-from-entry level after which a name never
    returned to its entry price? And is there a level it never closed
    back ABOVE once breached (an "EPmax")?

Q2  Portfolio: the -2% exit only fires in TIGHT-stop (5%) cycles, never in
    WIDE-stop (10%) ones. Wide-stop months are the beaten-down regime the
    strategy is built to buy the bounce in -- 2025-03 fell 7.58% below
    entry and finished +0.44%, so a flat rule sells that bottom.

State in /tmp/ep_rule.json.
"""
import datetime as dt
import json
import os
import sys

import pandas as pd

import harness                                              # noqa: E402
import strategy                                             # noqa: E402
import config                                               # noqa: E402

config.CORP_ACTION_GREY_ZONE_ENABLED = False

DOD = "/tmp/dod.json"
STOCK = "/tmp/ep_stock.json"


# ---------------------------------------------------------------------------
# Q1 -- per stock
# ---------------------------------------------------------------------------

def build_stock_paths():
    """{cycle: {symbol: [pct vs entry, per session]}} for every held name."""
    src = json.load(open(DOD))["rows"]
    out = json.load(open(STOCK)) if os.path.exists(STOCK) else {}
    if len(out) >= len(src):
        return out
    uni = harness.universe()
    for row in src:
        if row["k"] in out:
            continue
        y, m = map(int, row["k"].split("-"))
        ex, nx = harness.cycle_dates(y, m)
        merged = dict(strategy.load_price_history(nx, uni, days=90))
        merged.update(strategy.load_price_history(nx + dt.timedelta(days=12),
                                                  uni, days=20))
        after = [d for d in sorted(merged) if d > nx]
        roll = after[0] if after else nx
        days = [d for d in sorted(merged) if ex < d <= roll]
        stop_pct = row["stop"]
        cyc = {}
        for s in row["picks"]:
            f0 = merged.get(days[0]) if days else None
            if f0 is None or s not in f0.index:
                continue
            o = f0.at[s, "open_price"]
            o = float(o) if pd.notna(o) else None
            if not o or o <= 0:
                continue
            stop = o * (1 - stop_pct / 100)
            target = o * 1.40
            path, done = [], None
            for d in days:
                f = merged.get(d)
                if f is None or s not in f.index:
                    continue
                g = lambda c: (float(f.at[s, c]) if c in f.columns
                               and pd.notna(f.at[s, c]) else None)
                op, hi, lo, cl = g("open_price"), g("high_price"), g("low_price"), g("close_price")
                if cl is None:
                    continue
                op, hi, lo = (op or cl), (hi or cl), (lo or cl)
                if done is None:
                    if lo <= stop:
                        done = min(op, stop)
                    elif hi >= target:
                        done = max(op, target)
                px = done if done is not None else cl
                path.append(round((px / o - 1) * 100, 3))
            if path:
                cyc[s] = path
        out[row["k"]] = cyc
        json.dump(out, open(STOCK, "w"))       # save as we go
        print(f"  {row['k']}: {len(cyc)} paths", flush=True)
    return out


def q1(paths):
    obs = []          # one row per (stock, session)
    per_stock = []    # one row per stock
    for k, cyc in paths.items():
        for s, p in cyc.items():
            trough = min(p)
            ever_back = any(x >= 0 for x in p)
            per_stock.append({"k": k, "s": s, "trough": trough,
                              "final": p[-1], "ever_back": ever_back})
            for i, v in enumerate(p):
                later = p[i + 1:]
                obs.append({"v": v, "runway": len(later),
                            "back_to_ep": bool(later) and max(later) >= 0.0,
                            "above_again": bool(later) and max(later) > v})
    print(f"{len(per_stock)} stock-cycles, {len(obs)} stock-sessions\n")

    print("=== Q1a. closed X% below entry -> did it EVER get back to entry? ===")
    for t in [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30]:
        w = [o for o in obs if o["v"] <= -t and o["runway"] >= 1]
        if not w:
            print(f"  <= -{t:>2}%:   no observations")
            continue
        n = sum(1 for o in w if not o["back_to_ep"])
        flag = "   <-- 100%" if n == len(w) else ""
        print(f"  <= -{t:>2}%: {len(w):>5} obs, {n:>5} never back to EP "
              f"({n/len(w)*100:>5.1f}%){flag}")

    print("\n=== Q1b. EPmax: once below X%, did it ever close ABOVE X% again? ===")
    for t in [1, 2, 3, 5, 8, 10, 15, 20, 25, 30, 40]:
        breached = [(k, s, p) for k, cyc in paths.items()
                    for s, p in cyc.items() if min(p) <= -t]
        if not breached:
            print(f"  -{t:>2}%:  no stock ever breached")
            continue
        held = 0
        for _k, _s, p in breached:
            i = next(j for j, x in enumerate(p) if x <= -t)
            if not any(x > -t for x in p[i + 1:]):
                held += 1
        v = len(breached) - held
        flag = "   <-- never re-crossed" if v == 0 else ""
        print(f"  -{t:>2}%: {len(breached):>4} stocks breached, {v:>4} later "
              f"closed back above ({v/len(breached)*100:>5.1f}%){flag}")


# ---------------------------------------------------------------------------
# Q2 -- regime-conditional portfolio rule
# ---------------------------------------------------------------------------

def q2():
    rows = json.load(open(DOD))["rows"]
    print(f"{'cycle':<9}{'stop':>6}{'base':>9}{'flat -2%':>11}{'tight-only':>12}"
          f"   fired")
    tot = {"base": 0.0, "flat": 0.0, "cond": 0.0}
    for r in rows:
        curve = [v for _d, v in r["curve"]]
        base = r["final"]
        # exit at the CLOSE that first breaches -2%, book that level
        idx = next((i for i, v in enumerate(curve) if v <= -2.0), None)
        flat = curve[idx] if idx is not None else base
        cond = flat if (idx is not None and r["stop"] == 5.0) else base
        tot["base"] += base
        tot["flat"] += flat
        tot["cond"] += cond
        fired = ("flat+cond" if idx is not None and r["stop"] == 5.0
                 else "flat only" if idx is not None else "-")
        print(f"{r['k']:<9}{r['stop']:>5.0f}%{base:>8.2f}%{flat:>10.2f}%"
              f"{cond:>11.2f}%   {fired}")
    print(f"\n{'SUM':<9}{'':>6}{tot['base']:>8.2f}%{tot['flat']:>10.2f}%"
          f"{tot['cond']:>11.2f}%")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "portfolio"
    if what == "stock":
        q1(build_stock_paths())
    else:
        q2()
