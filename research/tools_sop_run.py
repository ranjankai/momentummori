"""
Run the ATR conviction-bucket SOP over one cycle.

    python tools_sop_run.py

Entry day is the expiry close; ATR20 is frozen on that day. Stops and
targets are multiples of the frozen ATR, scaled by conviction bucket.
The Initial Target never sells -- it only promotes INITIAL -> WINNER,
after which a ratchet stop trails the Highest High and can only rise.

Triggers are intraday (low vs stop, high vs target); execution is the
NEXT session's open, which is what the evening "EXIT TOMORROW AT MARKET
OPEN" instruction actually delivers.
"""
import datetime as dt
import json
import logging

import pandas as pd

logging.disable(logging.CRITICAL)
import strategy                                             # noqa: E402

ENTRY = dt.date(2026, 2, 24)
EXPIRY = dt.date(2026, 3, 30)
NEXT_OPEN = dt.date(2026, 4, 1)

# conviction order as supplied
TOP3 = ["NTPC", "ONGC", "PNB"]
MID4 = ["FEDERALBNK", "CUMMINSIND", "TATASTEEL", "BHARATFORG"]
BOT3 = ["TITAN", "IOC", "RBLBANK"]
PICKS = TOP3 + MID4 + BOT3

# bucket -> (stop ATR multiple, target ATR multiple)
MULT = {"Top 3": (2.5, 6.0), "Middle 4": (2.0, 5.0), "Bottom 3": (1.5, 4.0)}


def bucket(sym):
    return "Top 3" if sym in TOP3 else "Middle 4" if sym in MID4 else "Bottom 3"


def series(hist, sym, lo, hi):
    """[(date, open, high, low, close)] for sym over (lo, hi]."""
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


def run(atr, entry_px, bars, final_date):
    """Walk one position. Returns dict with exit reason, date, price, pct."""
    res = {}
    for sym in PICKS:
        b = bucket(sym)
        sm, tm = MULT[b]
        px = entry_px[sym]
        a = atr[sym]
        stop = px - sm * a
        target = px + tm * a
        state, hh, exit_flag = "INITIAL", px, False
        stop_path = [stop]
        promoted = None
        out = None

        for i, (d, o, h, l, c) in enumerate(bars[sym]):
            if exit_flag:                      # armed yesterday -> sell at open
                out = dict(reason=exit_flag, date=d, price=o)
                break
            if d > final_date:
                break
            # stop check first: a gap through the stop is a stop, not a target
            if l <= stop:
                exit_flag = "STOP" if state == "INITIAL" else "TRAIL"
                continue
            if state == "INITIAL" and h >= target:
                state = "WINNER"
                promoted = d
                hh = max(hh, h)
                stop = max(stop, hh - sm * a)
                stop_path.append(stop)
            elif state == "WINNER" and h > hh:
                hh = h
                stop = max(stop, hh - sm * a)
                stop_path.append(stop)

        res[sym] = dict(bucket=b, atr=a, entry=px, init_stop=px - sm * a,
                        target=target, state=state, hh=hh, final_stop=stop,
                        promoted=str(promoted) if promoted else None,
                        exit=out, stop_raises=len(stop_path) - 1)
    return res


def main():
    uni = strategy.load_fo_universe()
    hist = strategy.load_price_history(EXPIRY, uni, days=120)
    fwd = strategy.load_price_history(dt.date(2026, 4, 6), uni, days=30)
    merged = dict(hist)
    merged.update(fwd)

    # frozen ATR20 and entry close, both as of the entry day
    entry_px, atr = {}, {}
    days = [d for d in sorted(merged) if d <= ENTRY]
    for sym in PICKS:
        cl, hi, lo = [], [], []
        for d in days[-25:]:
            f = merged[d]
            if sym not in f.index:
                continue
            c = f.at[sym, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            cl.append(float(c))
            hi.append(float(f.at[sym, "high_price"]) if pd.notna(f.at[sym, "high_price"]) else float(c))
            lo.append(float(f.at[sym, "low_price"]) if pd.notna(f.at[sym, "low_price"]) else float(c))
        tr = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
              for i in range(1, len(cl))]
        atr[sym] = sum(tr[-20:]) / len(tr[-20:])

    # buy price is the OPEN of the first session after expiry (25-Feb),
    # not the expiry close -- ATR20 stays frozen on the expiry day
    bars = {s: series(merged, s, ENTRY, NEXT_OPEN) for s in PICKS}
    for sym in PICKS:
        entry_px[sym] = bars[sym][0][1]
    res = run(atr, entry_px, bars, EXPIRY)

    # mark-to-market closes for the two rollover conventions
    def px_on(sym, d, field):
        for dd, o, h, l, c in bars[sym]:
            if dd == d:
                return {"open": o, "close": c}[field]
        return None

    rows = []
    for sym in PICKS:
        r = res[sym]
        e = r["exit"]
        for label, d, field in [("expiry close", EXPIRY, "close"),
                                ("1-Apr open", NEXT_OPEN, "open")]:
            if e:
                exit_px, when, why = e["price"], e["date"], e["reason"]
            else:
                exit_px, when, why = px_on(sym, d, field), d, "ROLLOVER"
            rows.append(dict(symbol=sym, conv=label, bucket=r["bucket"],
                             entry=r["entry"], atr=r["atr"],
                             init_stop=r["init_stop"], target=r["target"],
                             state=r["state"], promoted=r["promoted"],
                             hh=r["hh"], final_stop=r["final_stop"],
                             exit_px=exit_px, exit_date=str(when), reason=why,
                             pct=(exit_px / r["entry"] - 1) * 100 if exit_px else None))
    json.dump(rows, open("/tmp/sop_run.json", "w"), default=str)

    for label in ("expiry close", "1-Apr open"):
        sub = [r for r in rows if r["conv"] == label]
        print(f"\n=== EXIT CONVENTION: {label} ===")
        print(f"{'symbol':<12}{'bucket':<10}{'entry':>9}{'ATR20':>8}{'stop':>9}"
              f"{'target':>9}{'state':>8}{'exit':>9}{'date':>12}{'reason':>10}{'ret':>8}")
        for r in sub:
            print(f"{r['symbol']:<12}{r['bucket']:<10}{r['entry']:>9.2f}{r['atr']:>8.2f}"
                  f"{r['init_stop']:>9.2f}{r['target']:>9.2f}{r['state']:>8}"
                  f"{r['exit_px']:>9.2f}{r['exit_date']:>12}{r['reason']:>10}"
                  f"{r['pct']:>7.2f}%")
        p = [r["pct"] for r in sub]
        print(f"{'PORTFOLIO (equal weight)':<40} {sum(p)/len(p):+.2f}%   "
              f"winners {sum(1 for x in p if x > 0)}/{len(p)}")


if __name__ == "__main__":
    main()
