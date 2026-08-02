"""
Build the seven-sheet feature workbook for one expiry date.

    python tools_features.py 2025-12-30 [out.xlsx]

Every column uses ONLY data available at that day's close. The three
prior monthly expiries supply the rollover/carry baselines, and they are
resolved through strategy.expiry_for so the holiday roll-back applies
(Mar-2026 expiry is the 30th, not the 31st: Mahavir Jayanti).
"""
import datetime as dt
import logging
import sys

import numpy as np
import pandas as pd

logging.disable(logging.CRITICAL)
import nse_client                                          # noqa: E402
import scoring                                             # noqa: E402
import strategy                                            # noqa: E402


def prior_expiries(d, n=3):
    """The n monthly expiries strictly before `d`, oldest first."""
    td = strategy.known_trading_days()
    out, y, m = [], d.year, d.month
    while len(out) < n:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        try:
            out.append(strategy.expiry_for(y, m, trading_days=td))
        except strategy.StrategyError:
            break
    return list(reversed(out))


def fo_snapshot(d):
    """Per-symbol derivatives readings at one expiry close."""
    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(d))
    fut = fo[fo["instrument_type"].isin(["STF", "FUTSTK"])].copy()
    cm = (scoring.normalize_cm_columns(nse_client.fetch_cm_bhavcopy(d))
          .drop_duplicates("symbol").set_index("symbol"))
    rec = {}
    for sym, g in fut.groupby("symbol"):
        g = g.sort_values("expiry_date")
        e = {f"oi{k}": float(g.iloc[k]["open_interest"])
             for k in range(min(3, len(g)))}
        if "volume" in g.columns:
            e["fvol"] = float(g["volume"].sum())
        if len(g) >= 2:
            t = g.iloc[0]["open_interest"] + g.iloc[1]["open_interest"]
            if t > 0:
                e["roll"] = g.iloc[1]["open_interest"] / t * 100
        fw = g[g["expiry_date"].dt.date > d]
        if len(fw) and sym in cm.index:
            n = fw.iloc[0]
            sp = cm.at[sym, "close_price"]
            dte = (n["expiry_date"].date() - d).days
            if pd.notna(sp) and sp > 0:
                e["basis"] = float(n["settlement_price"] - sp)
                e["carry"] = float((n["settlement_price"] - sp) / sp)
                if dte > 0:
                    e["acarry"] = e["carry"] * (365 / dte)
        rec[sym] = e
    return rec


def price_features(d, uni):
    hist = strategy.load_price_history(d, uni, days=90)
    days = [x for x in sorted(hist) if x <= d]
    rows = {}
    for s in uni:
        cl, hi, lo, vol = [], [], [], []
        for x in days:
            f = hist[x]
            if s not in f.index:
                continue
            c = f.at[s, "close_price"]
            if pd.isna(c) or c <= 0:
                continue
            cl.append(float(c))
            hi.append(float(f.at[s, "high_price"])
                      if pd.notna(f.at[s, "high_price"]) else float(c))
            lo.append(float(f.at[s, "low_price"])
                      if pd.notna(f.at[s, "low_price"]) else float(c))
            vol.append(float(f.at[s, "volume"])
                       if "volume" in f.columns and pd.notna(f.at[s, "volume"])
                       else np.nan)
        if len(cl) < 62:
            continue
        se = pd.Series(cl)
        c = cl[-1]
        tr = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
              for i in range(1, len(cl))]
        r = se.pct_change().dropna()
        d20, d50 = float(se.tail(20).mean()), float(se.tail(50).mean())
        rows[s] = dict(close=round(c, 2), r5=c / cl[-6] - 1, r20=c / cl[-21] - 1,
                       r60=c / cl[-61] - 1, dma20=round(d20, 2),
                       dma50=round(d50, 2), dist20=(c - d20) / d20,
                       dist50=(c - d50) / d50,
                       atr20=round(float(np.mean(tr[-20:])), 2),
                       hv20=float(r.tail(20).std() * np.sqrt(252)),
                       cashvol=float(vol[-1]) if vol and pd.notna(vol[-1]) else np.nan)
    return rows


def build(d, out):
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    tr = price_features(d, uni)
    uni = sorted(tr)
    prev = prior_expiries(d)
    cur = fo_snapshot(d)
    hist_fo = {p: fo_snapshot(p) for p in prev}
    last = hist_fo[prev[-1]] if prev else {}
    g = lambda s, k: cur.get(s, {}).get(k)

    t = pd.DataFrame([{
        "Symbol": s, "Sector": sec.get(s, "Unclassified"), "Close": tr[s]["close"],
        "5D Return": tr[s]["r5"], "20D Return": tr[s]["r20"], "60D Return": tr[s]["r60"],
        "20 DMA": tr[s]["dma20"], "50 DMA": tr[s]["dma50"],
        "Above20DMA": int(tr[s]["close"] > tr[s]["dma20"]),
        "Above50DMA": int(tr[s]["close"] > tr[s]["dma50"]),
        "Dist20DMA": tr[s]["dist20"], "Dist50DMA": tr[s]["dist50"],
        "ATR20": tr[s]["atr20"], "HV20": tr[s]["hv20"]} for s in uni])

    rr = []
    for s in uni:
        c = g(s, "roll")
        h = [hist_fo[p].get(s, {}).get("roll") for p in prev]
        h = [x for x in h if x is not None]
        a = float(np.mean(h)) if h else None
        sd = float(np.std(h, ddof=1)) if len(h) > 1 else None
        row = {"Symbol": s, "Current Roll": c}
        for p in prev:
            row[f"{p:%b} Roll"] = hist_fo[p].get(s, {}).get("roll")
        row.update({"Prev3M Avg Roll": a, "Prev3M StdDev": sd,
                    "Roll Surprise": (c - a) if c is not None and a is not None else None,
                    "Roll Z": ((c - a) / sd) if c is not None and a is not None and sd else None})
        rr.append(row)
    r2 = pd.DataFrame(rr)
    r2["Roll Rank"] = r2["Current Roll"].rank(ascending=False)

    r3 = pd.DataFrame([{
        "Symbol": s, "Carry": g(s, "carry"), "Annualized Carry": g(s, "acarry"),
        "Basis": g(s, "basis"),
        "Basis Change": (g(s, "basis") - last.get(s, {}).get("basis"))
        if g(s, "basis") is not None and last.get(s, {}).get("basis") is not None else None}
        for s in uni])
    r3["Carry Rank"] = r3["Carry"].rank(ascending=False)
    r3["Carry Percentile"] = r3["Carry"].rank(pct=True)
    r3["Carry Z"] = (r3["Carry"] - r3["Carry"].mean()) / r3["Carry"].std()

    oo = []
    for s in uni:
        c_, n_, f_ = g(s, "oi0"), g(s, "oi1"), g(s, "oi2")
        tot = sum(x for x in (c_, n_, f_) if x)
        pv = sum(x for x in (last.get(s, {}).get(k) for k in ("oi0", "oi1", "oi2")) if x)
        oo.append({"Symbol": s, "Current OI": c_, "Next OI": n_, "Far OI": f_,
                   "Roll Ratio": (n_ / (c_ + n_)) if c_ and n_ else None,
                   "Far Roll Ratio": (f_ / (n_ + f_)) if n_ and f_ else None,
                   "Total OI": tot or None,
                   "OI Change": ((tot - pv) / pv) if tot and pv else None})
    r4 = pd.DataFrame(oo)
    r4["OI Rank"] = r4["OI Change"].rank(ascending=False)

    # delivery % is in NSE's sec_bhavdata_full, not the UDiFF bhavcopy
    dlv = {}
    try:
        dd = nse_client.fetch_delivery_data(d)
        eq = dd[dd["SERIES"].isin(["EQ", "BE"])].drop_duplicates("SYMBOL").set_index("SYMBOL")
        for s in uni:
            if s in eq.index:
                dlv[s] = pd.to_numeric(eq.at[s, "DELIV_PER"], errors="coerce")
    except Exception as exc:
        logging.getLogger(__name__).warning("delivery data unavailable: %s", exc)

    r5 = pd.DataFrame([{"Symbol": s, "Futures Volume": g(s, "fvol"),
                        "Cash Volume": tr[s]["cashvol"],
                        "Delivery %": dlv.get(s)} for s in uni])
    r5["Futures Volume Rank"] = r5["Futures Volume"].rank(ascending=False)
    r5["Cash Volume Rank"] = r5["Cash Volume"].rank(ascending=False)
    r5["Cash/Futures Ratio"] = r5["Cash Volume"] / r5["Futures Volume"].replace(0, np.nan)
    r5 = r5[["Symbol", "Futures Volume", "Futures Volume Rank", "Cash Volume",
             "Cash Volume Rank", "Cash/Futures Ratio", "Delivery %"]]

    r6 = t[["Symbol"]].copy()
    r6["5D Rank"] = t["5D Return"].rank(ascending=False)
    r6["20D Rank"] = t["20D Return"].rank(ascending=False)
    r6["60D Rank"] = t["60D Return"].rank(ascending=False)
    r6["ATR Rank"] = t["ATR20"].rank(ascending=True)
    r6["Trend Composite"] = 0.5 * r6["60D Rank"] + 0.3 * r6["20D Rank"] + 0.2 * r6["5D Rank"]

    sg = t.groupby("Sector").agg(**{
        "Sector20DReturn": ("20D Return", "mean"),
        "Sector60DReturn": ("60D Return", "mean"),
        "SectorATR": ("ATR20", "mean")}).reset_index()
    sg["Sector Rank"] = sg["Sector60DReturn"].rank(ascending=False)
    r7 = t[["Symbol", "Sector", "60D Return"]].merge(sg, on="Sector", how="left")
    r7["Stock Rank Within Sector"] = r7.groupby("Sector")["60D Return"].rank(ascending=False)
    r7 = r7.drop(columns=["60D Return"])

    with pd.ExcelWriter(out) as w:
        for n, df in [("1 Trend", t), ("2 Rollover", r2), ("3 Carry", r3),
                      ("4 OI Migration", r4), ("5 Liquidity", r5),
                      ("6 Relative Strength", r6), ("7 Sector", r7)]:
            df.to_excel(w, sheet_name=n, index=False)
    return uni, prev, r2, r3, r5, r4


if __name__ == "__main__":
    d = dt.date.fromisoformat(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/features_{d:%d%b%Y}.xlsx"
    uni, prev, r2, r3, r5, r4 = build(d, out)
    print(f"{out}\nexpiry {d}   baselines {[str(p) for p in prev]}")
    print(f"rows {len(uni)}   roll {r2['Current Roll'].notna().sum()}"
          f"   carry {r3['Carry'].notna().sum()}"
          f"   futvol {r5['Futures Volume'].notna().sum()}"
          f"   OIchg {r4['OI Change'].notna().sum()}")
