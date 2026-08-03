"""
Run one named basket through BOTH exit rule sets and compare.

    python tools_cycle_compare.py

V4      regime stop (breadth-pegged 5%/10%), 40% target sells on touch
SOP     ATR20 stop and target scaled by conviction rank; the target only
        promotes INITIAL -> WINNER, then a ratchet stop trails the high

Both use the same buy price -- the OPEN of the first session after the
entry expiry -- and the same rollover exit, the first open after the next
expiry. Triggers are intraday; execution is the next session's open.
"""
import datetime as dt
import logging

import pandas as pd

logging.disable(logging.CRITICAL)
import strategy                                             # noqa: E402

ENTRY_EXPIRY = dt.date(2026, 3, 30)
NEXT_EXPIRY = dt.date(2026, 4, 28)

PICKS = ["PREMIERENE", "WAAREEENER", "MCX", "NATIONALUM", "COALINDIA",
         "DMART", "ONGC", "AUROPHARMA", "ADANIPOWER", "PERSISTENT"]
LABEL = {"PREMIERENE": "PREMIER ENERGIES", "WAAREEENER": "WAAREE ENERGIES",
         "NATIONALUM": "NALCO"}

MULT = [(2.5, 6.0)] * 3 + [(2.0, 5.0)] * 4 + [(1.5, 4.0)] * 3
V4_TARGET_PCT = 40.0


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


def atr20(hist, sym, upto):
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


def run_v4(seq, entry, stop_pct):
    stop = entry * (1 - stop_pct / 100)
    target = entry * (1 + V4_TARGET_PCT / 100)
    armed = None
    for d, o, h, l, c in seq:
        if armed:
            return o, d, armed, stop, target
        if l <= stop:
            armed = "STOP"
        elif h >= target:
            armed = "TARGET"
    return seq[-1][4], seq[-1][0], "ROLLOVER", stop, target


def run_sop(seq, entry, a, sm, tm):
    stop, target = entry - sm * a, entry + tm * a
    state, hh, armed, raises = "INITIAL", entry, None, 0
    for d, o, h, l, c in seq:
        if armed:
            return o, d, armed, stop, target, state, hh, raises
        if l <= stop:
            armed = "STOP" if state == "INITIAL" else "TRAIL"
            continue
        if state == "INITIAL" and h >= target:
            state, hh = "WINNER", max(hh, h)
            ns = hh - sm * a
            if ns > stop:
                stop, raises = ns, raises + 1
        elif state == "WINNER" and h > hh:
            hh = h
            ns = hh - sm * a
            if ns > stop:
                stop, raises = ns, raises + 1
    return seq[-1][4], seq[-1][0], "ROLLOVER", stop, target, state, hh, raises


def main():
    uni = strategy.load_fo_universe()
    # full window: market_breadth needs 200 sessions for the 200-DMA, and a
    # short load makes it return nan and silently fall back to the tight stop
    hist = strategy.load_price_history(ENTRY_EXPIRY, uni)
    merged = dict(hist)
    merged.update(strategy.load_price_history(NEXT_EXPIRY, uni, days=60))
    merged.update(strategy.load_price_history(NEXT_EXPIRY + dt.timedelta(days=12),
                                              uni, days=20))
    after = [d for d in sorted(merged) if d > NEXT_EXPIRY]
    roll = after[0] if after else NEXT_EXPIRY

    breadth = strategy.market_breadth(ENTRY_EXPIRY, uni, hist)
    stop_pct = strategy.resolve_stop_pct(ENTRY_EXPIRY, uni, hist)
    print(f"entry expiry {ENTRY_EXPIRY:%d-%m-%y}   next expiry {NEXT_EXPIRY:%d-%m-%y}"
          f"   rollover {roll:%d-%m-%y}")
    print(f"breadth {breadth:.1f}%  ->  V4 regime stop {stop_pct:.0f}%\n")

    v4r, sopr = [], []
    print("=== V4: regime stop + 40% target ===")
    print(f"{'name':<18}{'entry':>10}{'stop':>10}{'target':>11}{'exit':>10}"
          f"{'date':>12}{'reason':>10}{'ret':>9}")
    rows = {}
    for rank, sym in enumerate(PICKS):
        seq = bars(merged, sym, ENTRY_EXPIRY, roll)
        if not seq:
            print(f"{LABEL.get(sym, sym):<18} no data")
            continue
        entry = seq[0][1]
        px, d, why, stop, tgt = run_v4(seq[1:], entry, stop_pct)
        pct = (px / entry - 1) * 100
        v4r.append(pct)
        rows[sym] = dict(entry=entry, seq=seq, rank=rank)
        print(f"{LABEL.get(sym, sym):<18}{entry:>10.2f}{stop:>10.2f}{tgt:>11.2f}"
              f"{px:>10.2f}{str(d):>12}{why:>10}{pct:>8.2f}%")
    print(f"{'PORTFOLIO':<18}{'':>43}{sum(v4r)/len(v4r):>38.2f}%")

    print("\n=== SOP: ATR conviction stop + ratchet ===")
    print(f"{'name':<18}{'bkt':>5}{'entry':>10}{'ATR20':>9}{'stop':>10}{'target':>11}"
          f"{'exit':>10}{'date':>12}{'reason':>9}{'st':>8}{'ret':>9}")
    for sym, r in rows.items():
        rank, seq, entry = r["rank"], r["seq"], r["entry"]
        a = atr20(merged, sym, ENTRY_EXPIRY)
        sm, tm = MULT[rank]
        b = "Top3" if rank < 3 else "Mid4" if rank < 7 else "Bot3"
        px, d, why, stop, tgt, state, hh, raises = run_sop(seq[1:], entry, a, sm, tm)
        pct = (px / entry - 1) * 100
        sopr.append(pct)
        print(f"{LABEL.get(sym, sym):<18}{b:>5}{entry:>10.2f}{a:>9.2f}"
              f"{entry - sm * a:>10.2f}{tgt:>11.2f}{px:>10.2f}{str(d):>12}"
              f"{why:>9}{state:>8}{pct:>8.2f}%")
    print(f"{'PORTFOLIO':<18}{'':>76}{sum(sopr)/len(sopr):>13.2f}%")

    print(f"\nV4 {sum(v4r)/len(v4r):+.2f}%   SOP {sum(sopr)/len(sopr):+.2f}%   "
          f"diff {sum(sopr)/len(sopr) - sum(v4r)/len(v4r):+.2f}pp")


if __name__ == "__main__":
    main()
