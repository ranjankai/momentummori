"""
Is there a day-on-day drop after which the cycle never recovers?

    python research/dod_threshold.py          # advance one cycle
    python research/dod_threshold.py report

Builds the V4 book's DAILY equity curve -- equal weight across ten slots,
a slot's return frozen once it stops out or hits target -- then asks, for
every down day, whether the book ever regained that day's opening level
before the cycle ended.

"Recovered" = the equity curve, at any later point in the cycle, got back
to where it stood the session BEFORE the drop. That is the honest test of
"was this ripple or a step change".

Cycles are entered at the open after the stated expiry and closed at the
open after the next one -- the live convention.

State in /tmp/dod.json.
"""
import datetime as dt
import json
import os
import sys

import pandas as pd

import harness                                              # noqa: E402
import strategy                                             # noqa: E402
import config                                               # noqa: E402

config.CORP_ACTION_GREY_ZONE_ENABLED = False    # deterministic, offline

STATE = "/tmp/dod.json"

MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3), (2026, 4), (2026, 6)]


def _cycles():
    out = []
    for y, m in MONTHS:
        try:
            ex, nx = harness.cycle_dates(y, m)
        except Exception:
            continue
        out.append((f"{y}-{m:02d}", ex, nx))
    return out


CYCLES = _cycles()

# A drop in the last few sessions has no runway to recover, which is an
# artefact of the calendar rather than evidence of a step change. Only
# drops with at least this many sessions left are scored.
MIN_RUNWAY = 5


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def equity_curve(picks, merged, ex, roll, stop_pct, target_pct=40.0):
    """
    [(date, portfolio_pct)] for every session in the holding window.

    Entry is the first session's OPEN. A slot that exits keeps its realised
    return for the rest of the cycle -- cash, exactly as the live rules say
    (redeployment is off).
    """
    days = [d for d in sorted(merged) if ex < d <= roll]
    if not days:
        return []
    pos = {}
    for s in picks:
        f = merged.get(days[0])
        if f is None or s not in f.index:
            continue
        o = f.at[s, "open_price"]
        o = float(o) if pd.notna(o) else None
        if not o or o <= 0:
            continue
        pos[s] = {"entry": o, "stop": o * (1 - stop_pct / 100),
                  "target": o * (1 + target_pct / 100),
                  "done": None, "last": o}

    out = []
    for d in days:
        f = merged.get(d)
        for s, p in pos.items():
            if p["done"] is not None or f is None or s not in f.index:
                continue
            g = lambda c: (float(f.at[s, c]) if c in f.columns
                           and pd.notna(f.at[s, c]) else None)
            o, hi, lo, cl = g("open_price"), g("high_price"), g("low_price"), g("close_price")
            if cl is None:
                continue
            o, hi, lo = (o or cl), (hi or cl), (lo or cl)
            if lo <= p["stop"]:
                p["done"] = min(o, p["stop"])
            elif hi >= p["target"]:
                p["done"] = max(o, p["target"])
            else:
                p["last"] = cl
        tot = 0.0
        for p in pos.values():
            px = p["done"] if p["done"] is not None else p["last"]
            tot += (px / p["entry"] - 1) * 100
        out.append((d, tot / max(len(pos), 1)))
    return out


def analyse(curve):
    """For every down day: the drop, and whether the book recovered later."""
    rows = []
    for i in range(1, len(curve)):
        prev, cur = curve[i - 1][1], curve[i][1]
        drop = cur - prev                       # in percentage POINTS
        if drop >= 0:
            continue
        later = [v for _d, v in curve[i + 1:]]
        recovered = bool(later) and max(later) >= prev
        rows.append({"date": str(curve[i][0]), "drop_pp": drop,
                     "level_before": prev, "level_after": cur,
                     "best_later": max(later) if later else None,
                     "recovered": recovered, "runway": len(later)})
    return rows


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        allrows = [dict(r, cycle=row["k"]) for row in s["rows"]
                   for r in row["drops"] if r.get("runway", 99) >= MIN_RUNWAY]
        if not allrows:
            return
        allrows.sort(key=lambda r: r["drop_pp"])
        rec = [r for r in allrows if r["recovered"]]
        non = [r for r in allrows if not r["recovered"]]
        print(f"{len(s['rows'])} cycles, {len(allrows)} down days with at "
              f"least {MIN_RUNWAY} sessions left")
        print(f"  recovered {len(rec)}   never recovered {len(non)}")
        if rec:
            print(f"  LARGEST drop that DID recover:      "
                  f"{min(r['drop_pp'] for r in rec):.2f} pp")
        if non:
            print(f"  SMALLEST drop that NEVER recovered: "
                  f"{max(r['drop_pp'] for r in non):.2f} pp")

        print("\n--- the ten worst single days, and what followed ---")
        print(f"{'cycle':<9}{'date':<12}{'drop pp':>9}{'before':>9}"
              f"{'best later':>12}{'runway':>8}{'recovered':>11}")
        for r in allrows[:10]:
            print(f"{r['cycle']:<9}{r['date']:<12}{r['drop_pp']:>8.2f}"
                  f"{r['level_before']:>9.2f}{(r['best_later'] or 0):>12.2f}"
                  f"{r['runway']:>8}{('YES' if r['recovered'] else 'no'):>11}")

        print("\n--- is there a level above which recovery NEVER happened? ---")
        for t in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
            worse = [r for r in allrows if r["drop_pp"] <= -t]
            if not worse:
                continue
            n = sum(1 for r in worse if not r["recovered"])
            flag = "  <-- 100% failure" if n == len(worse) else ""
            print(f"  drops >= {t:.1f} pp: {len(worse):>3} days, "
                  f"{n:>3} never recovered  ({n/len(worse)*100:>5.1f}%){flag}")
        cut = None
        for t in [x / 10 for x in range(5, 81)]:
            worse = [r for r in allrows if r["drop_pp"] <= -t]
            if worse and all(not r["recovered"] for r in worse):
                cut = t
                break
        print(f"\nSmallest threshold with a 100% failure rate: "
              f"{cut if cut else 'NONE -- no such level exists'}"
              + (f" pp  (n={len([r for r in allrows if r['drop_pp'] <= -cut])})"
                 if cut else ""))
        return

    i = s["i"]
    if i >= len(CYCLES):
        print("complete -- run `report`")
        return
    k, ex, nx = CYCLES[i]
    uni = harness.universe()
    hist = strategy.load_price_history(ex, uni)
    stop_pct = strategy.resolve_stop_pct(ex, uni, hist)
    d = strategy.basket_for(ex, uni, harness.sectors(), price_hist=hist)

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12),
                                              uni, days=20))
    after = [x for x in sorted(merged) if x > nx]
    roll = after[0] if after else nx

    curve = equity_curve(d.symbols, merged, ex, roll, stop_pct)
    drops = analyse(curve)
    s["rows"].append({"k": k, "stop": stop_pct, "final": curve[-1][1] if curve else 0.0,
                      "curve": [(str(a), round(b, 3)) for a, b in curve],
                      "drops": drops, "picks": d.symbols})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{k}  stop {stop_pct:.0f}%  final {curve[-1][1]:+.2f}%  "
          f"sessions {len(curve)}  down days {len(drops)}   [{i+1}/{len(CYCLES)}]")


if __name__ == "__main__":
    main()
