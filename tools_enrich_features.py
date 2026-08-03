"""
Enrich an existing seven-sheet feature workbook with the mandatory blocks.

    python tools_enrich_features.py 2026-02-24 <in.xlsx> [out.xlsx]

Adds, one row per stock:
  * 6M average rollover and the 6M roll surprise (the shipped workbook
    only carried a 3M baseline)
  * OI build-up classification from the sign pair (price change, OI change)
  * ASM/GSM, T2T and F&O-ban flags
  * NIFTY 50 / 100 / 200 membership

Stages cache to /tmp so a 45s shell timeout cannot lose work.

CAVEAT ON AS-OF ACCURACY
  The F&O ban flag is date-exact -- NSE archives the ban list per trade
  date. The ASM/GSM list and the index membership lists are CURRENT
  snapshots; NSE does not archive them per date. For a Feb-2026 workbook
  read those three columns as "status today", not "status on 24-Feb-2026".
"""
import datetime as dt
import io
import json
import logging
import os
import sys

import numpy as np
import pandas as pd

logging.disable(logging.CRITICAL)
import nse_client                                           # noqa: E402
import nse_corporate                                        # noqa: E402
import scoring                                              # noqa: E402
import strategy                                             # noqa: E402
import tools_features                                       # noqa: E402

STAGE_A = "/tmp/enrich_roll.json"
STAGE_B = "/tmp/enrich_flags.json"

INDEX_LISTS = {
    "NIFTY 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY 100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY 200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
}
EQUITY_L = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BAN_URL = ("https://nsearchives.nseindia.com/archives/fo/sec_ban/"
           "fo_secban_{date:%d%m%Y}.csv")


def _csv(url):
    return pd.read_csv(io.BytesIO(nse_client._download_with_retry(url)))


def stage_roll(d):
    """6M rollover baseline + expiry-to-expiry price change, per symbol."""
    if os.path.exists(STAGE_A):
        return json.load(open(STAGE_A))
    prev = tools_features.prior_expiries(d, n=6)
    cur = tools_features.fo_snapshot(d)
    hist = {str(p): tools_features.fo_snapshot(p) for p in prev}

    cm_now = (scoring.normalize_cm_columns(nse_client.fetch_cm_bhavcopy(d))
              .drop_duplicates("symbol").set_index("symbol"))
    cm_prev = (scoring.normalize_cm_columns(nse_client.fetch_cm_bhavcopy(prev[-1]))
               .drop_duplicates("symbol").set_index("symbol"))

    out = {}
    for sym in set(cur) | {s for h in hist.values() for s in h}:
        rolls = [hist[str(p)].get(sym, {}).get("roll") for p in prev]
        rolls = [r for r in rolls if r is not None]
        c = cur.get(sym, {}).get("roll")
        avg6 = float(np.mean(rolls)) if rolls else None
        pc = None
        if sym in cm_now.index and sym in cm_prev.index:
            a, b = cm_prev.at[sym, "close_price"], cm_now.at[sym, "close_price"]
            if pd.notna(a) and pd.notna(b) and a > 0:
                pc = float(b / a - 1)
        out[sym] = {
            "roll6_n": len(rolls),
            "roll6_avg": avg6,
            "roll6_surprise": (c - avg6) if c is not None and avg6 is not None else None,
            "price_chg": pc,
        }
    json.dump({"prev": [str(p) for p in prev], "data": out}, open(STAGE_A, "w"))
    return json.load(open(STAGE_A))


def stage_flags(d):
    """ASM/GSM, T2T, F&O ban, index membership."""
    if os.path.exists(STAGE_B):
        return json.load(open(STAGE_B))
    out = {"asm": {}, "t2t": [], "ban": [], "idx": {}, "notes": []}

    try:
        out["asm"] = {k: str(v) for k, v in nse_corporate.fetch_asm_symbols().items()}
    except Exception as exc:
        out["notes"].append(f"ASM/GSM unavailable: {exc}")

    try:
        eq = _csv(EQUITY_L)
        eq.columns = [c.strip() for c in eq.columns]
        ser = eq.set_index("SYMBOL")["SERIES"].astype(str).str.strip()
        out["t2t"] = sorted(ser[ser.isin(["BE", "BZ"])].index.tolist())
    except Exception as exc:
        out["notes"].append(f"T2T series unavailable: {exc}")

    try:
        raw = nse_client._download_with_retry(BAN_URL.format(date=d)).decode("utf8")
        out["ban"] = [ln.split(",")[1].strip() for ln in raw.splitlines()
                      if "," in ln and ln.split(",")[0].strip().isdigit()]
    except Exception as exc:
        out["notes"].append(f"F&O ban list unavailable: {exc}")

    for name, url in INDEX_LISTS.items():
        try:
            out["idx"][name] = sorted(_csv(url)["Symbol"].astype(str).str.strip().tolist())
        except Exception as exc:
            out["notes"].append(f"{name} membership unavailable: {exc}")

    json.dump(out, open(STAGE_B, "w"))
    return out


def classify_oi(price_chg, oi_chg):
    """Standard four-quadrant F&O read on the (price, OI) sign pair."""
    if price_chg is None or oi_chg is None or pd.isna(price_chg) or pd.isna(oi_chg):
        return None
    if price_chg >= 0 and oi_chg >= 0:
        return "Long Build-up"
    if price_chg < 0 and oi_chg >= 0:
        return "Short Build-up"
    if price_chg >= 0 and oi_chg < 0:
        return "Short Covering"
    return "Long Unwinding"


def build(d, src, out):
    book = {s: pd.read_excel(src, sheet_name=s)
            for s in pd.ExcelFile(src).sheet_names}
    roll = stage_roll(d)
    flags = stage_flags(d)
    rd = roll["data"]

    t, r2, r3 = book["1 Trend"], book["2 Rollover"], book["3 Carry"]
    r4, r5, r6 = book["4 OI Migration"], book["5 Liquidity"], book["6 Relative Strength"]
    r7 = book["7 Sector"]

    # --- extend sheet 2 with the 6M baseline -------------------------------
    r2["Prev6M Avg Roll"] = r2["Symbol"].map(lambda s: rd.get(s, {}).get("roll6_avg"))
    r2["Prev6M Months Used"] = r2["Symbol"].map(lambda s: rd.get(s, {}).get("roll6_n"))
    r2["Roll Surprise 6M"] = r2["Symbol"].map(lambda s: rd.get(s, {}).get("roll6_surprise"))

    # --- extend sheet 4 with the build-up read -----------------------------
    pc = r4["Symbol"].map(lambda s: rd.get(s, {}).get("price_chg"))
    r4["Price Change (expiry to expiry)"] = pc
    r4["OI Build-up"] = [classify_oi(p, o) for p, o in zip(pc, r4["OI Change"])]

    # --- new sheet 8: universe filters -------------------------------------
    asm, t2t, ban = flags["asm"], set(flags["t2t"]), set(flags["ban"])
    idx = {k: set(v) for k, v in flags["idx"].items()}
    sec = dict(zip(t["Symbol"], t["Sector"]))
    r8 = pd.DataFrame({"Symbol": t["Symbol"]})
    r8["Sector"] = r8["Symbol"].map(sec)
    r8["ASM/GSM Flag"] = r8["Symbol"].map(lambda s: "Y" if s in asm else "N")
    r8["ASM/GSM Stage"] = r8["Symbol"].map(lambda s: asm.get(s))
    r8["T2T Flag"] = r8["Symbol"].map(lambda s: "Y" if s in t2t else "N")
    r8["F&O Ban Flag"] = r8["Symbol"].map(lambda s: "Y" if s in ban else "N")
    for k in ("NIFTY 50", "NIFTY 100", "NIFTY 200"):
        r8[k] = r8["Symbol"].map(lambda s, kk=k: "Y" if s in idx.get(kk, ()) else "N")
    r8["Index Tier"] = [
        "NIFTY 50" if a == "Y" else "NIFTY 100" if b == "Y"
        else "NIFTY 200" if c == "Y" else "Outside NIFTY 200"
        for a, b, c in zip(r8["NIFTY 50"], r8["NIFTY 100"], r8["NIFTY 200"])]
    r8["Market Cap"] = np.nan
    r8["Free Float Market Cap"] = np.nan
    r8["Tradeable"] = [
        "N" if (x == "Y" or y == "Y" or z == "Y") else "Y"
        for x, y, z in zip(r8["ASM/GSM Flag"], r8["T2T Flag"], r8["F&O Ban Flag"])]

    # --- new sheet 0: one flat row per stock -------------------------------
    m = (t[["Symbol", "Sector", "Close", "Above20DMA", "Above50DMA", "ATR20", "HV20"]]
         .merge(r6[["Symbol", "5D Rank", "20D Rank", "60D Rank", "Trend Composite"]], on="Symbol", how="left")
         .merge(r5[["Symbol", "Cash Volume Rank", "Futures Volume Rank", "Delivery %"]], on="Symbol", how="left")
         .merge(r2[["Symbol", "Current Roll", "Prev3M Avg Roll", "Prev6M Avg Roll",
                    "Roll Surprise", "Roll Surprise 6M"]], on="Symbol", how="left")
         .merge(r3[["Symbol", "Carry", "Basis"]], on="Symbol", how="left")
         .merge(r4[["Symbol", "OI Change", "OI Rank", "OI Build-up"]], on="Symbol", how="left")
         .merge(r8[["Symbol", "ASM/GSM Flag", "T2T Flag", "F&O Ban Flag", "Tradeable",
                    "NIFTY 50", "NIFTY 100", "NIFTY 200", "Index Tier",
                    "Market Cap", "Free Float Market Cap"]], on="Symbol", how="left"))
    m = m.rename(columns={"5D Rank": "5D RS Rank", "20D Rank": "20D RS Rank",
                          "60D Rank": "60D RS Rank", "Roll Surprise": "Roll Surprise 3M"})
    m["Above20DMA"] = np.where(m["Above20DMA"] == 1, "Y", "N")
    m["Above50DMA"] = np.where(m["Above50DMA"] == 1, "Y", "N")

    notes = pd.DataFrame({"Note": [
        f"Expiry: {d:%d-%m-%y}",
        f"Rollover baselines used: {', '.join(roll['prev'])}",
        "F&O ban flag is date-exact (NSE archives fo_secban per trade date).",
        "ASM/GSM, T2T and NIFTY membership are CURRENT snapshots -- NSE does "
        "not archive these per date. Read as status today, not 24-02-26.",
        "Market Cap and Free Float Market Cap are EMPTY: no accessible source. "
        "NSE quote-equity API returns 403 from this environment and the archive "
        "weightage files 404. Needs a paid/alternate feed.",
        "OI Build-up uses the sign pair (expiry-to-expiry price change, OI change): "
        "up/up=Long Build-up, down/up=Short Build-up, up/down=Short Covering, "
        "down/down=Long Unwinding.",
    ] + flags.get("notes", [])})

    with pd.ExcelWriter(out) as w:
        m.to_excel(w, sheet_name="0 Master", index=False)
        for n, df in [("1 Trend", t), ("2 Rollover", r2), ("3 Carry", r3),
                      ("4 OI Migration", r4), ("5 Liquidity", r5),
                      ("6 Relative Strength", r6), ("7 Sector", r7),
                      ("8 Universe Filters", r8)]:
            df.to_excel(w, sheet_name=n, index=False)
        notes.to_excel(w, sheet_name="9 Notes", index=False)
    return m, r8, flags


if __name__ == "__main__":
    d = dt.date.fromisoformat(sys.argv[1])
    src = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else src.replace(".xlsx", "_enriched.xlsx")
    m, r8, flags = build(d, src, out)
    print(out)
    print(f"master rows {len(m)}  cols {len(m.columns)}")
    print(f"roll6 {m['Prev6M Avg Roll'].notna().sum()}  "
          f"buildup {m['OI Build-up'].notna().sum()}  "
          f"delivery {m['Delivery %'].notna().sum()}")
    print(f"ASM {(r8['ASM/GSM Flag'] == 'Y').sum()}  T2T {(r8['T2T Flag'] == 'Y').sum()}  "
          f"ban {(r8['F&O Ban Flag'] == 'Y').sum()}  "
          f"N50 {(r8['NIFTY 50'] == 'Y').sum()}  N100 {(r8['NIFTY 100'] == 'Y').sum()}  "
          f"N200 {(r8['NIFTY 200'] == 'Y').sum()}")
    for n in flags.get("notes", []):
        print("WARN", n)
