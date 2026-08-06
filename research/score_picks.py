"""
Run the PICKING_METHOD baskets through OUR exit rules.

    python research/score_picks.py          # advance one month
    python research/score_picks.py report

Entry is the OPEN of the first session AFTER their selection date -- the
date the method actually chooses on, which is the last session before the
1st of the month, not our expiry. Exit is the OPEN of the first session
after that month's F&O expiry. Regime stop resolved on the selection
date, 40% target, fresh-start additive, no carry.

Alongside each, the V4 engine's own basket over the IDENTICAL window, so
the only difference is which ten names were held.

State in /tmp/score_picks.json.
"""
import datetime as dt
import json
import os
import statistics as st
import sys

import harness                                              # noqa: E402
import strategy                                             # noqa: E402

STATE = "/tmp/score_picks.json"

# (label, selection date, that month's expiry, the ten names)
MONTHS = [
    ("2026-02", dt.date(2026, 1, 30), dt.date(2026, 2, 24),
     ["NATIONALUM", "UNIONBANK", "OIL", "VEDL", "BANKINDIA",
      "ASHOKLEY", "SBIN", "MCX", "HINDALCO", "BEL"]),
    ("2026-03", dt.date(2026, 2, 27), dt.date(2026, 3, 30),
     ["KEI", "POLYCAB", "BHARATFORG", "POWERINDIA", "AMBER",
      "GVT&D", "VOLTAS", "FORCEMOT", "CUMMINSIND", "UNIONBANK"]),
    ("2026-04", dt.date(2026, 3, 30), dt.date(2026, 4, 28),
     ["PREMIERENE", "GODFRYPHLP", "PAGEIND", "KAYNES", "WAAREEENER",
      "OFSS", "INOXWIND", "COFORGE", "KALYANKJIL", "TRENT"]),
    ("2026-07", dt.date(2026, 6, 30), dt.date(2026, 7, 28),
     ["INDIGO", "GMRAIRPORT", "RADICO", "CHOLAFIN", "LAURUSLABS",
      "TRENT", "LTF", "FEDERALBNK", "NYKAA", "PRESTIGE"]),
]


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'month':<9}{'stop':>6}{'PICKS':>9}{'V4':>9}{'diff':>8}"
              f"{'n':>4}{'stops':>7}")
        for r in rows:
            print(f"{r['k']:<9}{r['stop']:>5.0f}%{r['picks']:>8.2f}%"
                  f"{r['v4']:>8.2f}%{r['picks'] - r['v4']:>7.2f}{r['n']:>4}"
                  f"{r['stopped']:>7}")
        if rows:
            p = [r["picks"] for r in rows]
            v = [r["v4"] for r in rows]
            print(f"\nPICKS  sum {sum(p):+7.2f}%   mean {st.mean(p):5.2f}%   "
                  f"worst {min(p):6.2f}%   positive {sum(1 for x in p if x > 0)}/{len(p)}")
            print(f"V4     sum {sum(v):+7.2f}%   mean {st.mean(v):5.2f}%   "
                  f"worst {min(v):6.2f}%   positive {sum(1 for x in v if x > 0)}/{len(v)}")
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    key, sel, nx, picks = MONTHS[i]

    uni = harness.universe()
    hist = strategy.load_price_history(sel, uni)
    stop_pct = strategy.resolve_stop_pct(sel, uni, hist)

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12),
                                              uni, days=20))

    res = harness.run_cycle(picks, sel, nx, stop_pct=stop_pct,
                            price_by_date=merged)

    # V4's own ten over the SAME window, so only the names differ
    v4_picks, v4_ranked, _s, _b = harness.v4_basket(
        strategy.expiry_for(sel.year, sel.month,
                            trading_days=strategy.known_trading_days())
        if sel.day > 20 else sel)
    v4res = harness.run_cycle(v4_picks, sel, nx, stop_pct=stop_pct,
                              ranked_order=v4_ranked, price_by_date=merged)

    s["rows"].append({"k": key, "stop": stop_pct,
                      "picks": res.return_pct, "v4": v4res.return_pct,
                      "n": len(picks), "stopped": res.trades,
                      "v4_names": v4_picks})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{key}  entry after {sel}  exit after {nx}  stop {stop_pct:.0f}%  "
          f"PICKS {res.return_pct:+.2f}%   V4 {v4res.return_pct:+.2f}%")


if __name__ == "__main__":
    main()
