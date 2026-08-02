"""
Manual day-by-day walk-forward for one cycle.

Shows ONE day at a time and nothing beyond it, so an off-momentum
judgement cannot be made with knowledge of what happens next. State is
persisted between invocations; each call advances exactly one session.

    python tools_dayrun.py show          # readings for the current day
    python tools_dayrun.py exit S3 S7    # sell these at the NEXT open
    python tools_dayrun.py next          # advance one day
"""
import datetime
import json
import logging
import sys

logging.disable(logging.CRITICAL)
import strategy                                            # noqa: E402

STATE = "/tmp/apr_run.json"


def load():
    s = json.load(open(STATE))
    s["days"] = [datetime.date.fromisoformat(d) for d in s["days"]]
    return s


def save(s):
    out = dict(s)
    out["days"] = [str(d) for d in s["days"]]
    json.dump(out, open(STATE, "w"))


def hist_for(syms, upto):
    return strategy.load_price_history(upto, syms, days=45)


def main():
    s = load()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    i = s["day_idx"]
    days = s["days"]
    d = days[i]
    st = s["state"]
    syms = [p["sym"] for p in st.values()]
    if not syms:
        print("no open positions")
        return

    h = hist_for(syms, d)
    fr = h[d]

    if cmd == "exit":
        for lab in sys.argv[2:]:
            if lab in st:
                st[lab]["pending_exit"] = True
                print(f"queued OFF-MOMENTUM exit: {lab} ({st[lab]['sym']})")
        save(s)
        return

    if cmd == "next":
        s["day_idx"] = i + 1
        if s["day_idx"] >= len(days):
            print("cycle complete")
            save(s)
            return
        nd = days[s["day_idx"]]
        nfr = hist_for(syms, nd)[nd]
        # queued off-momentum sells fill at THIS open
        for lab in list(st):
            if st[lab].get("pending_exit"):
                p = st[lab]
                px = float(nfr.at[p["sym"], "open_price"])
                s.setdefault("cash", {})[lab] = p["cap"] * (px / p["e"])
                s.setdefault("log", []).append(
                    [str(nd), lab, p["sym"], "OFF_MOM",
                     round((px / p["e"] - 1) * 100, 2)])
                del st[lab]
        # stop / target on the new day
        for lab in list(st):
            p = st[lab]
            if p["sym"] not in nfr.index:
                continue
            lo = float(nfr.at[p["sym"], "low_price"])
            hi = float(nfr.at[p["sym"], "high_price"])
            px, tag = (p["st"], "STOP") if lo <= p["st"] else (
                (p["tg"], "TARGET") if hi >= p["tg"] else (None, None))
            if px:
                s.setdefault("cash", {})[lab] = p["cap"] * (px / p["e"])
                s.setdefault("log", []).append(
                    [str(nd), lab, p["sym"], tag,
                     round((px / p["e"] - 1) * 100, 2)])
                del st[lab]
        save(s)
        print(f"advanced to {nd}")
        return

    # --- show -------------------------------------------------------------
    print(f"=== DAY {i + 1}/{len(days)}  {d} ===")
    print(f"{'lab':<5}{'close':>10}{'frm entry':>10}{'to stop':>9}"
          f"{'to tgt':>8}{'>dma20':>8}{'>dma50':>8}{'r5d':>7}{'r21d':>8}")
    closes = {}
    for lab, p in sorted(st.items()):
        sym = p["sym"]
        if sym not in fr.index:
            continue
        c = float(fr.at[sym, "close_price"])
        closes[lab] = c
        ser = []
        for dd in sorted(h):
            if dd > d:
                break
            if sym in h[dd].index:
                v = h[dd].at[sym, "close_price"]
                if v and v > 0:
                    ser.append(float(v))
        import pandas as pd
        se = pd.Series(ser)
        d20 = se.tail(20).mean() if len(se) >= 20 else None
        d50 = se.tail(50).mean() if len(se) >= 50 else None
        r5 = (c / ser[-6] - 1) * 100 if len(ser) > 6 else None
        r21 = (c / ser[-22] - 1) * 100 if len(ser) > 22 else None
        f = lambda v, w=7: (f"{v:>{w}.1f}" if v is not None else " " * w)
        print(f"{lab:<5}{c:>10,.2f}{(c/p['e']-1)*100:>9.1f}%"
              f"{(c/p['st']-1)*100:>8.1f}%{(p['tg']/c-1)*100:>7.1f}%"
              f"{((c/d20-1)*100 if d20 else 0):>7.1f}%"
              f"{((c/d50-1)*100 if d50 else 0):>7.1f}%{f(r5)}{f(r21,8)}")
    val = sum(s.get("cash", {}).values()) + sum(
        p["cap"] * (closes[lab] / p["e"]) for lab, p in st.items() if lab in closes)
    print(f"\nopen {len(st)}   portfolio Rs {val:.2f}")


if __name__ == "__main__":
    main()
