"""
2x2 matrix: {V4 entry, v1.1 entry} x {V4 exit, ATR SOP exit}, 13 cycles.

    python tools_matrix.py          # advance one cycle (all 4 cells)
    python tools_matrix.py report   # print the tables

EVERY CELL IS FRESH-START AND ADDITIVE
  Rs100 is deployed on the first session after expiry and fully closed at
  the first session after the next expiry. Nothing is carried. Monthly
  returns are summed, never compounded.

ENTRY RULES
  V4    0.50*z(volatility) + 0.30*z(rollover) + 0.20*z(cost of carry),
        max 2 per sector, top 10.
  v1.1  equal-weight blend of three blocks, max 3 per sector, top 10
        drawn from the top 50 candidates:
          Derivative  = mean z of (roll surprise, carry, OI change,
                                   futures volume)
          Volatility  = -mean z of (ATR20/price, HV20)   [rewards CALM]
          Trend       = 0.50*z(60D) + 0.30*z(20D) + 0.20*z(5D)
        mandatory: close > 20 DMA and close > 50 DMA.

EXIT RULES
  V4    breadth-pegged stop (5% / 10%), 40% target sells on touch
  SOP   stop/target are ATR20 multiples by conviction rank
        (rank 1-3: 2.5x/6x, 4-7: 2.0x/5x, 8-10: 1.5x/4x); the target only
        promotes INITIAL -> WINNER, then a ratchet stop trails the high

Triggers are intraday; execution is the next session's open.

KNOWN LIMITS
  * ASM/GSM is a CURRENT snapshot at NSE, not archived per date, so it is
    NOT applied historically -- doing so would leak look-ahead. The
    date-exact F&O ban list IS applied.
  * T2T is structurally impossible inside the F&O universe, so the filter
    is a no-op by construction.
  * Step 8's LLM fallback is not invoked; short months are reported.

State lives in /tmp/matrix.json, per-expiry features in /tmp/feat_*.json.
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
import tools_features                                       # noqa: E402

STATE = "/tmp/matrix.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]
SLOTS = 10
MULT = [(2.5, 6.0)] * 3 + [(2.0, 5.0)] * 4 + [(1.5, 4.0)] * 3
V4_TARGET_PCT = 40.0
BAN_URL = ("https://nsearchives.nseindia.com/archives/fo/sec_ban/"
           "fo_secban_{date:%d%m%Y}.csv")
CELLS = ["V4entry_V4exit", "V4entry_SOPexit", "V11entry_SOPexit", "V11entry_V4exit"]


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": [], "detail": []}


def z(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std()
    return (s - s.mean()) / sd if sd and sd > 0 else s * 0.0


def banned_on(d):
    try:
        raw = nse_client._download_with_retry(BAN_URL.format(date=d)).decode("utf8")
        return {ln.split(",")[1].strip() for ln in raw.splitlines()
                if "," in ln and ln.split(",")[0].strip().isdigit()}
    except Exception:
        return set()


def features(d, uni):
    """Per-symbol v1.1 inputs at expiry `d`. Cached to /tmp."""
    cache = f"/tmp/feat_{d:%Y%m%d}.json"
    if os.path.exists(cache):
        return pd.read_json(cache, orient="split")

    prev = tools_features.prior_expiries(d, n=3)
    cur = tools_features.fo_snapshot(d)
    hist_fo = {str(p): tools_features.fo_snapshot(p) for p in prev}
    last = hist_fo[str(prev[-1])] if prev else {}
    px = tools_features.price_features(d, uni)

    rows = []
    for s, t in px.items():
        c = cur.get(s, {})
        rolls = [hist_fo[str(p)].get(s, {}).get("roll") for p in prev]
        rolls = [r for r in rolls if r is not None]
        avg = float(np.mean(rolls)) if rolls else None
        cr = c.get("roll")
        tot = sum(x for x in (c.get("oi0"), c.get("oi1"), c.get("oi2")) if x)
        pv = sum(x for x in (last.get(s, {}).get(k) for k in ("oi0", "oi1", "oi2")) if x)
        rows.append(dict(
            symbol=s, close=t["close"], dma20=t["dma20"], dma50=t["dma50"],
            r5=t["r5"], r20=t["r20"], r60=t["r60"], atr20=t["atr20"], hv20=t["hv20"],
            roll=cr, roll_surprise=(cr - avg) if cr is not None and avg is not None else None,
            carry=c.get("carry"), fvol=c.get("fvol"),
            oi_change=((tot - pv) / pv) if tot and pv else None))
    df = pd.DataFrame(rows)
    df.to_json(cache, orient="split", index=False)
    return df


def rank_v11(df, sec, ban):
    """v1.1 selection. Returns list of up to 10 symbols, best first."""
    d = df.copy()
    d = d[~d["symbol"].isin(ban)]
    d = d[(d["close"] > d["dma20"]) & (d["close"] > d["dma50"])]
    d = d.dropna(subset=["roll_surprise", "carry", "oi_change", "fvol",
                         "atr20", "hv20", "r5", "r20", "r60"])
    if d.empty:
        return [], 0
    deriv = (z(d["roll_surprise"]) + z(d["carry"]) + z(d["oi_change"])
             + z(d["fvol"])) / 4
    vol = -(z(d["atr20"] / d["close"]) + z(d["hv20"])) / 2
    trend = 0.50 * z(d["r60"]) + 0.30 * z(d["r20"]) + 0.20 * z(d["r5"])
    d["score"] = (deriv + vol + trend) / 3
    d = d.sort_values("score", ascending=False).head(50)

    picks, counts = [], {}
    for _, r in d.iterrows():
        s = sec.get(r["symbol"], f"Unclassified:{r['symbol']}")
        if counts.get(s, 0) >= 3:
            continue
        picks.append(r["symbol"])
        counts[s] = counts.get(s, 0) + 1
        if len(picks) >= SLOTS:
            break
    return picks, len(d)


def bars(hist, sym, lo, hi):
    out = []
    for d in sorted(hist):
        if not (lo < d <= hi):
            continue
        f = hist[d]
        if sym not in f.index:
            continue
        c = f.at[sym, "close_price"]
        if pd.isna(c) or c <= 0:
            continue
        g = lambda k: (float(f.at[sym, k]) if k in f.columns
                       and pd.notna(f.at[sym, k]) else float(c))
        out.append((d, g("open_price"), g("high_price"), g("low_price"), float(c)))
    return out


def atr20_at(hist, sym, upto):
    cl, hi, lo = [], [], []
    for d in [x for x in sorted(hist) if x <= upto][-25:]:
        f = hist[d]
        if sym not in f.index:
            continue
        c = f.at[sym, "close_price"]
        if pd.isna(c) or c <= 0:
            continue
        cl.append(float(c))
        hi.append(float(f.at[sym, "high_price"]) if pd.notna(f.at[sym, "high_price"]) else float(c))
        lo.append(float(f.at[sym, "low_price"]) if pd.notna(f.at[sym, "low_price"]) else float(c))
    if len(cl) < 21:
        return None
    tr = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
          for i in range(1, len(cl))]
    return sum(tr[-20:]) / 20


def exit_v4(seq, entry, stop_pct):
    stop, target = entry * (1 - stop_pct / 100), entry * (1 + V4_TARGET_PCT / 100)
    armed = None
    for d, o, h, l, c in seq:
        if armed:
            return o, d, armed
        if l <= stop:
            armed = "STOP"
        elif h >= target:
            armed = "TARGET"
    return seq[-1][1], seq[-1][0], "ROLLOVER"


def exit_sop(seq, entry, a, sm, tm):
    stop, target = entry - sm * a, entry + tm * a
    state, hh, armed = "INITIAL", entry, None
    for d, o, h, l, c in seq:
        if armed:
            return o, d, armed
        if l <= stop:
            armed = "STOP" if state == "INITIAL" else "TRAIL"
            continue
        if state == "INITIAL" and h >= target:
            state, hh = "WINNER", max(hh, h)
            stop = max(stop, hh - sm * a)
        elif state == "WINNER" and h > hh:
            hh = h
            stop = max(stop, hh - sm * a)
    return seq[-1][1], seq[-1][0], "ROLLOVER"


def run_basket(picks, merged, ex, roll, stop_pct):
    """Return (v4_pct, sop_pct, n) for one basket under both exit rules."""
    v4, sop = [], []
    for rank, sym in enumerate(picks):
        seq = bars(merged, sym, ex, roll)
        if len(seq) < 2:
            continue
        entry = seq[0][1]
        a = atr20_at(merged, sym, ex)
        if not a:
            continue
        px, _, _ = exit_v4(seq[1:], entry, stop_pct)
        v4.append((px / entry - 1) * 100)
        sm, tm = MULT[min(rank, SLOTS - 1)]
        px2, _, _ = exit_sop(seq[1:], entry, a, sm, tm)
        sop.append((px2 / entry - 1) * 100)
    n = len(v4)
    return (sum(v4) / SLOTS if n else 0.0, sum(sop) / SLOTS if n else 0.0, n)


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        hdr = ("1) V4+V4", "2) V4+ATR", "3) v1.1+ATR", "4) v1.1+V4")
        print(f"{'cycle':<9}{'breadth':>8}{'stop':>5}" + "".join(f"{h:>13}" for h in hdr))
        for r in rows:
            print(f"{r['k']:<9}{r['breadth']:>8.1f}{r['stop']:>5.0f}"
                  + "".join(f"{r[c]:>12.2f}%" for c in CELLS))
        print()
        for name, c in zip(hdr, CELLS):
            x = [r[c] for r in rows]
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5) if len(x) > 1 else float("nan")
            print(f"{name:<13} sum {sum(x):>+7.2f}%   mean {st.mean(x):>5.2f}%   "
                  f"sd {st.stdev(x):>5.2f}   worst {min(x):>6.2f}%   "
                  f"best {max(x):>6.2f}%   pos {sum(1 for v in x if v > 0):>2}/{len(x)}   "
                  f"t {t:>5.2f}")
        short = [(r["k"], r["n11"]) for r in rows if r["n11"] < SLOTS]
        if short:
            print(f"\nv1.1 short months (Step 8 would fire): {short}")
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
    stop_pct = strategy.resolve_stop_pct(ex, uni, hist)

    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, _ = strategy.rank_universe(sig, sec)
    picks_v4 = basket["symbol"].tolist()[:SLOTS]

    df = features(ex, uni)
    picks_v11, npool = rank_v11(df, sec, banned_on(ex))

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12), uni, days=20))
    after = [d for d in sorted(merged) if d > nx]
    roll = after[0] if after else nx

    a4, a_sop, n4 = run_basket(picks_v4, merged, ex, roll, stop_pct)
    b4, b_sop, n11 = run_basket(picks_v11, merged, ex, roll, stop_pct)

    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": breadth, "stop": stop_pct,
                      "V4entry_V4exit": a4, "V4entry_SOPexit": a_sop,
                      "V11entry_SOPexit": b_sop, "V11entry_V4exit": b4,
                      "n4": n4, "n11": n11, "pool": npool,
                      "picks_v4": picks_v4, "picks_v11": picks_v11})
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    r = s["rows"][-1]
    print(f"{r['k']}  breadth {breadth:.1f}% stop {stop_pct:.0f}%  |  "
          f"V4+V4 {a4:+.2f}%  V4+ATR {a_sop:+.2f}%  "
          f"v11+ATR {b_sop:+.2f}%  v11+V4 {b4:+.2f}%  "
          f"(n11={n11}, pool={npool})   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
