"""
The canonical 13-cycle backtest. One convention, one number.

    python research/run13.py          # advance one cycle
    python research/run13.py report   # print the table

Replaces `tools_run13.py`, retired 03-Aug-2026. That script chained
carry-forward across expiries, which was superseded when the reporting
convention was fixed as FRESH-START AND ADDITIVE: Rs100 is deployed on
the first session after an expiry, fully closed at the first session
after the next, and monthly returns are summed, never compounded.
Carry-forward remains an operational detail for the Telegram messages --
it decides whether a held name generates a SELL+BUY pair or nothing --
but it is not a backtest assumption.

The walk is delegated to `strategy.simulate_month` via `harness.py`.
Nothing here reimplements stops, targets, fills or corporate actions.

State lives in /tmp/run13_fresh.json so a shell timeout cannot lose work.
"""
import json
import os
import statistics as st
import sys

import harness                                              # noqa: E402

STATE = "/tmp/run13_fresh.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'cycle':<9}{'breadth':>9}{'stop':>6}{'return':>9}{'trades':>8}")
        for r in rows:
            print(f"{r['k']:<9}{r['breadth']:>9.1f}{r['stop']:>6.0f}"
                  f"{r['ret']:>8.2f}%{r['trades']:>8}")
        if rows:
            x = [r["ret"] for r in rows]
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5) if len(x) > 1 else float("nan")
            print(f"\nFRESH-START ADDITIVE   sum {sum(x):+.2f}%")
            print(f"mean {st.mean(x):.2f}%/mo | sd {st.stdev(x):.2f} | "
                  f"worst {min(x):.2f}% | best {max(x):.2f}% | "
                  f"positive {sum(1 for v in x if v > 0)}/{len(x)} | t {t:.2f}")
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    y, m = MONTHS[i]
    ex, nx = harness.cycle_dates(y, m)
    picks, ranked, stop_pct, breadth = harness.v4_basket(ex)
    res = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                            ranked_order=ranked)
    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": breadth,
                      "stop": stop_pct, "ret": res.return_pct,
                      "trades": res.trades, "picks": picks})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{y}-{m:02d}  breadth {breadth:.1f}%  stop {stop_pct:.0f}%  "
          f"return {res.return_pct:+.2f}%  trades {res.trades}   "
          f"[{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
