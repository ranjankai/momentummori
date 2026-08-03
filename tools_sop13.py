"""
Walk-forward the ATR conviction SOP over 13 cycles WITH carry-forward.

    python tools_sop13.py          # advance one cycle
    python tools_sop13.py report   # print the table

Selection is the LIVE engine (strategy.rank_universe, sector cap applied),
so baskets are identical to V4's. Only the exit logic differs.

CARRY-FORWARD
  A held name that reappears in the new basket is NOT sold and NOT reset.
  Its entry price, frozen ATR20, Highest High, State and ratchet stop all
  survive the expiry untouched -- resetting them would widen the stop,
  which the hard rules forbid. Only the return accounting is re-marked:
  the new cycle measures from the rollover price, so cycles chain without
  double-counting.

  A held name absent from the new basket exits at the rollover open.
  Freed slots take new picks at the rollover open. Unfilled slots are
  cash and score 0% for the cycle.

Buy price is the OPEN of the first session after expiry. ATR20 is frozen
on the entry day. Rollover is the first open after the next expiry.

State lives in /tmp/sop13.json.
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

STATE = "/tmp/sop13.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]
SLOTS = 10

# rank 1-3 / 4-7 / 8-10 -> (stop ATR mult, target ATR mult)
MULT = [(2.5, 6.0)] * 3 + [(2.0, 5.0)] * 4 + [(1.5, 4.0)] * 3


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": [], "detail": [], "carry": {}}


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


def walk(seq, pos):
    """
    Advance one position across `seq`. Mutates pos in place.
    Returns (exit_price, exit_date, reason) or (None, None, None) if alive.
    """
    sm, tm = pos["sm"], pos["tm"]
    a, entry = pos["atr"], pos["entry"]
    target = entry + tm * a
    armed = None
    for d, o, h, l, c in seq:
        if armed:
            return o, d, armed
        if l <= pos["stop"]:
            armed = "STOP" if pos["state"] == "INITIAL" else "TRAIL"
            continue
        if pos["state"] == "INITIAL" and h >= target:
            pos["state"] = "WINNER"
            pos["promoted"] = str(d)
            pos["hh"] = max(pos["hh"], h)
            ns = pos["hh"] - sm * a
            if ns > pos["stop"]:
                pos["stop"], pos["raises"] = ns, pos["raises"] + 1
        elif pos["state"] == "WINNER" and h > pos["hh"]:
            pos["hh"] = h
            ns = pos["hh"] - sm * a
            if ns > pos["stop"]:
                pos["stop"], pos["raises"] = ns, pos["raises"] + 1
    return None, None, None


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'cycle':<9}{'breadth':>9}{'SOP ret':>10}{'new':>5}{'carried':>9}"
              f"{'stops':>7}{'promoted':>10}{'cash':>6}")
        for r in rows:
            print(f"{r['k']:<9}{r['breadth']:>9.1f}{r['sop']:>9.2f}%{r['new']:>5}"
                  f"{r['carried']:>9}{r['stopped']:>7}{r['promoted']:>10}{r['cash']:>6}")
        if rows:
            x = [r["sop"] for r in rows]
            acc = 100.0
            for v in x:
                acc *= (1 + v / 100)
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5) if len(x) > 1 else float("nan")
            print(f"\nSOP+carry  sum {sum(x):+.2f}%  |  Rs100 -> {acc:.2f}  |  "
                  f"mean {st.mean(x):.2f}%  sd {st.stdev(x):.2f}  "
                  f"worst {min(x):.2f}%  best {max(x):.2f}%  "
                  f"positive {sum(1 for v in x if v > 0)}/{len(x)}  t {t:.2f}")
            print(f"promotions to WINNER: {sum(r['promoted'] for r in rows)}")
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
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, _ = strategy.rank_universe(sig, sec)
    picks = basket["symbol"].tolist()[:SLOTS]

    merged = dict(hist)
    merged.update(strategy.load_price_history(nx, uni, days=60))
    merged.update(strategy.load_price_history(nx + dt.timedelta(days=12), uni, days=20))
    after = [d for d in sorted(merged) if d > nx]
    roll = after[0] if after else nx

    carry = s["carry"]
    rets, det = [], []
    stopped = promoted = 0
    new_pos, carried, out_carry = 0, 0, {}

    # --- 1. held names not in the new basket exit at the rollover open ----
    for sym, pos in list(carry.items()):
        if sym in picks:
            continue
        seq = bars(merged, sym, ex, roll)
        px = seq[0][1] if seq else pos["mark"]
        rets.append((px / pos["mark"] - 1) * 100)
        det.append(dict(k=f"{y}-{m:02d}", sym=sym, reason="DROPPED",
                        pct=round((px / pos["mark"] - 1) * 100, 2)))
        carry.pop(sym)

    # --- 2. walk every slot through the cycle ----------------------------
    held = list(carry)
    fresh = [p for p in picks if p not in carry]
    seq_start = ex

    for sym in held + fresh:
        seq = bars(merged, sym, seq_start, roll)
        if not seq:
            continue
        if sym in carry:
            pos = carry[sym]
            basis = pos["mark"]                 # re-mark: chain from last cycle
            carried += 1
            walk_seq = seq
        else:
            a = atr20_at(merged, sym, ex)
            if not a:
                continue
            rank = picks.index(sym)
            sm, tm = MULT[min(rank, SLOTS - 1)]
            entry = seq[0][1]
            pos = dict(sym=sym, entry=entry, atr=a, sm=sm, tm=tm, rank=rank + 1,
                       state="INITIAL", hh=entry, stop=entry - sm * a,
                       promoted=None, raises=0, mark=entry)
            basis = entry
            new_pos += 1
            walk_seq = seq[1:]

        px, d, reason = walk(walk_seq, pos)
        if px is None:                          # alive at rollover
            px, d, reason = seq[-1][1], seq[-1][0], "HELD"
            pos["mark"] = px
            out_carry[sym] = pos
        else:
            stopped += 1
        if pos["promoted"] and pos["promoted"].startswith(f"{y}-{m:02d}"):
            promoted += 1
        pct = (px / basis - 1) * 100
        rets.append(pct)
        det.append(dict(k=f"{y}-{m:02d}", sym=sym, rank=pos["rank"],
                        entry=round(pos["entry"], 2), atr=round(pos["atr"], 2),
                        basis=round(basis, 2), exit=round(px, 2), date=str(d),
                        reason=reason, pct=round(pct, 2), state=pos["state"],
                        raises=pos["raises"]))

    cash = SLOTS - len(rets)
    port = sum(rets) / SLOTS if rets else 0.0   # unfilled slots earn 0

    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": breadth, "sop": port,
                      "new": new_pos, "carried": carried, "stopped": stopped,
                      "promoted": promoted, "cash": max(cash, 0)})
    s["detail"].extend(det)
    s["carry"] = out_carry
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    r = s["rows"][-1]
    print(f"{r['k']}  breadth {breadth:.1f}%  SOP {port:+.2f}%  new {new_pos}  "
          f"carried {carried}  stopped {stopped}  promoted {promoted}  "
          f"carrying {len(out_carry)}   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
