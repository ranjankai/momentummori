"""
In WIDE-stop cycles, does an extra close-based exit help or hurt?

    python research/wide_close_exit.py

Wide-stop (10%) cycles deliberately give a name room, because breadth was
low and the strategy is buying a bounce. The per-stock EP analysis showed
that once a name CLOSES 5% below entry it almost never returns to entry
(1 of 1080 observations) -- but most of those observations come from
exactly these wide-stop cycles, so the finding may just be describing the
room being given, not an opportunity.

This adds, for wide-stop cycles only, a close-based exit at -X% executed
at the NEXT session's open, on top of the existing 10% intraday stop and
40% target. Sweeps X. Tight-stop cycles are untouched throughout.

The three wide cycles are 2025-03 (+0.44%), 2026-01 (+12.13%) and
2026-03 (+22.90%) -- the strategy's best months. If a close-based exit
damages those, the room is doing its job.

Reads the per-stock paths already built by ep_rule.py (/tmp/ep_stock.json)
and the cycle metadata from dod_threshold.py (/tmp/dod.json). Both are
returns-vs-entry series with the live stop/target already applied, which
is what an additional CLOSE rule has to be layered onto.
"""
import json
import os
import statistics as st

DOD = "/tmp/dod.json"
STOCK = "/tmp/ep_stock.json"
LEVELS = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


def main():
    meta = {r["k"]: r for r in json.load(open(DOD))["rows"]}
    paths = json.load(open(STOCK))

    print("Per-stock paths are returns vs entry with the live intraday stop "
          "and 40% target already applied.\nThe extra rule: in WIDE-stop "
          "cycles only, if a name CLOSES <= -X%, exit at the next close.\n")

    hdr = ["base"] + [f"{int(x)}%" for x in LEVELS]
    print(f"{'cycle':<9}{'stop':>6}" + "".join(f"{h:>9}" for h in hdr))

    tot = {h: 0.0 for h in hdr}
    for k in sorted(paths):
        m = meta[k]
        wide = m["stop"] == 10.0
        cyc = paths[k]
        row = {}
        for h, lvl in [("base", None)] + list(zip(hdr[1:], LEVELS)):
            rets = []
            for _s, p in cyc.items():
                if lvl is None or not wide:
                    rets.append(p[-1])
                    continue
                idx = next((i for i, v in enumerate(p) if v <= -lvl), None)
                if idx is None or idx + 1 >= len(p):
                    rets.append(p[-1])
                else:
                    rets.append(p[idx + 1])       # exit at the next close
            row[h] = sum(rets) / 10.0             # ten slots, cash otherwise
            tot[h] += row[h]
        mark = " <- WIDE" if wide else ""
        print(f"{k:<9}{m['stop']:>5.0f}%"
              + "".join(f"{row[h]:>8.2f}%" for h in hdr) + mark)

    print()
    print(f"{'SUM':<9}{'':>6}" + "".join(f"{tot[h]:>8.2f}%" for h in hdr))
    print(f"{'vs base':<9}{'':>6}" + "".join(
        f"{tot[h]-tot['base']:>8.2f} " for h in hdr))

    print("\n--- wide-stop cycles only ---")
    wide_keys = [k for k in paths if meta[k]["stop"] == 10.0]
    for h in hdr:
        s = 0.0
        for k in wide_keys:
            cyc = paths[k]
            lvl = None if h == "base" else float(h.rstrip("%"))
            rets = []
            for _s, p in cyc.items():
                if lvl is None:
                    rets.append(p[-1])
                    continue
                idx = next((i for i, v in enumerate(p) if v <= -lvl), None)
                rets.append(p[-1] if idx is None or idx + 1 >= len(p)
                            else p[idx + 1])
            s += sum(rets) / 10.0
        print(f"  {h:<6} {s:>8.2f}%   ({', '.join(wide_keys)})")


if __name__ == "__main__":
    main()
