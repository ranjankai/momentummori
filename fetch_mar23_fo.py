"""
Fetch the F&O bhavcopy for 23-Mar-2026 -- the session before Altcase's
MAR_APRIL basket was entered (24-Mar open).

Why this date matters: it sits FOUR sessions before the 30-Mar monthly
expiry, i.e. mid-rollover. Every other snapshot we have is either on
expiry day or just after it, so this is the first look at rollover % and
cost of carry during the roll window itself.

Run from the momentum-tracker project root:

    python fetch_mar23_fo.py

Verify TradDt reads 2026-03-23 before trusting the output.
"""
import sys
import traceback
from datetime import date

sys.path.insert(0, ".")

import nse_client

TARGET = date(2026, 3, 23)

try:
    df = nse_client.fetch_fo_bhavcopy(TARGET)
except Exception as exc:
    print(f"FAILED for {TARGET:%d-%b-%Y}: {exc}")
    traceback.print_exc(limit=1)
    sys.exit(1)

print(f"OK  rows={len(df)}  cached at data/cache/fo_{TARGET:%Y%m%d}.csv")

if "TradDt" in df.columns:
    print("TradDt:", df["TradDt"].unique())
if "FinInstrmTp" in df.columns:
    stf = df[df["FinInstrmTp"].isin(["STF", "FUTSTK"])]
    print(f"stock-futures rows: {len(stf)}  symbols: {stf['TckrSymb'].nunique()}")
    if "XpryDt" in stf.columns:
        print("expiries present:", sorted(stf["XpryDt"].astype(str).unique())[:4])
        print("  (expect 2026-03-30 near, 2026-04-28 next, 2026-05-26 far)")
