"""
Fetch the March-2025 monthly-expiry F&O bhavcopy (27-Mar-2025, last
Thursday -- pre-dates NSE's Sep-2025 switch to last Tuesday).

This is the snapshot the April-2025 basket is selected from, and it is
the only month missing from the 13-month backtest.

Run from the momentum-tracker project root:

    python fetch_mar2025_fo.py
"""
import sys
import traceback
from datetime import date

sys.path.insert(0, ".")

import nse_client

d = date(2025, 3, 27)
try:
    df = nse_client.fetch_fo_bhavcopy(d)
except Exception as exc:
    print(f"FAILED for {d:%d-%b-%Y}: {exc}")
    traceback.print_exc(limit=1)
    sys.exit(1)

print(f"OK  rows={len(df)}  cached at data/cache/fo_{d:%Y%m%d}.csv")
if "TradDt" in df.columns:
    print("TradDt:", df["TradDt"].unique())
if "FinInstrmTp" in df.columns:
    print("stock-futures rows:", int(df["FinInstrmTp"].isin(["STF", "FUTSTK"]).sum()))
