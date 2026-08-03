"""
Isolate SELECTION: their ten names vs our ten, same window, same rules.

    python tools_selection_test.py          # advance one cycle
    python tools_selection_test.py report

Both baskets are entered at the OPEN of the first session after expiry
and exited at the OPEN of the first session after the next expiry, under
the identical live regime stop and 40% target. Nothing else differs, so
the whole gap is stock choice.

This is the test that settles whether our selection is the problem. The
earlier Altcase comparisons leaned on their published entry prices, whose
dates may not line up with our expiry windows -- that flaw is removed
here because their names are re-entered on OUR dates at OUR prices.

State lives in /tmp/seltest.json.
"""
import datetime as dt
import json
import logging
import os
import statistics as st
import sys

logging.disable(logging.CRITICAL)
import nse_client                                           # noqa: E402
import scoring                                              # noqa: E402
import strategy                                             # noqa: E402
from tools_matrix import bars                               # noqa: E402
from tools_stopsweep import walk                            # noqa: E402

STATE = "/tmp/seltest.json"
MONTHS = [(2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8), (2025, 9),
          (2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3)]
SLOTS = 10

ALIAS = {
    "FEDERALBK": "FEDERALBNK", "ADANISOL": "ADANIENSOL", "RBLBK": "RBLBANK",
    "RBKBK": "RBLBANK", "DALMIA BH": "DALMIABHA", "DALMIABH": "DALMIABHA",
    "DALMIACEM": "DALMIABHA", "DIIVISLAB": "DIVISLAB", "ADANIPORT": "ADANIPORTS",
    "HEROMTR": "HEROMOTOCO", "BAJAJFIN": "BAJFINANCE", "NALCO": "NATIONALUM",
    "PHONEIX MILL": "PHOENIXLTD", "PHONIX MILL": "PHOENIXLTD",
    "INDUSTO": "INDUSTOWER", "AVENUE SUPERMARTS": "DMART",
    "PREMIER ENERGIES": "PREMIERENE", "WAAREE ENERGIES": "WAAREEENER",
    "JSPL": "JINDALSTEL", "AMBUACEM": "AMBUJACEM", "AMBERENT": "AMBER",
    "PNBHSGFIN": "PNBHOUSING", "BLUESTAR": "BLUESTARCO", "BOSCH": "BOSCHLTD",
    "HUL": "HINDUNILVR", "OBEROIRTY": "OBEROIRLTY", "GMRAIRPORT": "GMRAIRPORT",
    "SOLARINDS": "SOLARINDS", "INDIANBK": "INDIANB",
}


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": []}


def to_symbol(name, uni):
    n = name.strip().upper()
    if n in ALIAS and ALIAS[n] in uni:
        return ALIAS[n]
    if n in uni:
        return n
    c = [u for u in uni if u.startswith(n[:6])]
    return c[0] if len(c) == 1 else None


def score(picks, merged, ex, roll, stop_pct):
    tot, n, det = 0.0, 0, []
    for s in picks:
        q = bars(merged, s, ex, roll)
        if len(q) < 2:
            continue
        e = q[0][1]
        px, why = walk(q[1:], e, stop_pct)
        r = (px / e - 1) * 100
        tot += r
        n += 1
        det.append((s, round(r, 2), why))
    return (tot / SLOTS if n else 0.0), n, det


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'cycle':<9}{'stop':>5}{'ours':>9}{'theirs':>9}{'gap':>8}"
              f"{'n':>4}{'overlap':>9}")
        for r in rows:
            print(f"{r['k']:<9}{r['stop']:>4.0f}%{r['ours']:>8.2f}%{r['theirs']:>8.2f}%"
                  f"{r['theirs'] - r['ours']:>7.2f}{r['n']:>4}{r['overlap']:>9}")
        o = [r["ours"] for r in rows]
        t = [r["theirs"] for r in rows]
        print(f"\nOURS    sum {sum(o):+7.2f}%  mean {st.mean(o):5.2f}%  sd {st.stdev(o):5.2f}  "
              f"worst {min(o):6.2f}%  pos {sum(1 for v in o if v > 0)}/{len(o)}")
        print(f"THEIRS  sum {sum(t):+7.2f}%  mean {st.mean(t):5.2f}%  sd {st.stdev(t):5.2f}  "
              f"worst {min(t):6.2f}%  pos {sum(1 for v in t if v > 0)}/{len(t)}")
        d = [t[i] - o[i] for i in range(len(o))]
        tt = st.mean(d) / (st.stdev(d) / len(d) ** 0.5)
        print(f"GAP     sum {sum(d):+7.2f}pp  mean {st.mean(d):5.2f}pp  "
              f"their basket wins {sum(1 for x in d if x > 0)}/{len(d)}  t {tt:.2f}")
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    y, m = MONTHS[i]
    alt = json.load(open("/tmp/alt.json"))
    key = f"{y}-{m:02d}"
    td = strategy.known_trading_days()
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    ex = strategy.expiry_for(y, m, trading_days=td)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nx = strategy.expiry_for(ny, nm, trading_days=td)

    hist = strategy.load_price_history(ex, uni)
    stop_pct = strategy.resolve_stop_pct(ex, uni, hist)
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, _ = strategy.rank_universe(sig, sec)
    ours = basket["symbol"].tolist()[:SLOTS]

    uset = set(uni)
    theirs = [to_symbol(x, uset) for x in alt.get(key, [])]
    theirs = [x for x in theirs if x]

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12), uni, days=20))
    roll = [d for d in sorted(merged) if d > nx][0]

    a, na, da = score(ours, merged, ex, roll, stop_pct)
    b, nb, db = score(theirs, merged, ex, roll, stop_pct)

    s["rows"].append({"k": key, "stop": stop_pct, "ours": a, "theirs": b,
                      "n": nb, "overlap": len(set(ours) & set(theirs)),
                      "det_ours": da, "det_theirs": db,
                      "picks_ours": ours, "picks_theirs": theirs})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{key}  stop {stop_pct:.0f}%  ours {a:+.2f}%  theirs {b:+.2f}%  "
          f"gap {b - a:+.2f}pp  (their n={nb}, overlap={len(set(ours) & set(theirs))})"
          f"   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
