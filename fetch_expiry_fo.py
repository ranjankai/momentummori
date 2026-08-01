"""
Fetch the monthly-expiry F&O bhavcopies needed for the 13-month backtest
(Apr 2025 - Apr 2026).

Expiry convention: last Thursday of the month up to Aug 2025, last Tuesday
from Sep 2025 onward (NSE rule change effective 01-Sep-2025). Where the
expiry fell on a holiday it has already been rolled back to the previous
real trading day.

Run from the momentum-tracker project root:

    python fetch_expiry_fo.py

Safe to re-run: anything already cached is skipped. Failures are logged
and do not stop the remaining downloads -- rerun to retry just the
stragglers.
"""
import sys
import time
import traceback
from datetime import date

sys.path.insert(0, ".")

import nse_client

DATES = [
    date(2025, 4, 24),
    date(2025, 5, 29),
    date(2025, 6, 26),
    date(2025, 8, 28),
    date(2025, 10, 28),
    date(2025, 11, 25),
    date(2025, 12, 30),
    date(2026, 1, 27),
    date(2026, 4, 28),
]

PAUSE_SECONDS = 1.5  # be polite to NSE between requests

ok, failed = [], []
for i, d in enumerate(DATES, 1):
    tag = f"[{i}/{len(DATES)}] {d:%d-%b-%Y}"
    try:
        df = nse_client.fetch_fo_bhavcopy(d)
        traded = df["TradDt"].unique() if "TradDt" in df.columns else ["?"]
        stf = (df["FinInstrmTp"] == "STF").sum() if "FinInstrmTp" in df.columns else 0
        print(f"{tag}  OK   rows={len(df):>6}  TradDt={traded}  stock-futures rows={stf}")
        ok.append(d)
    except Exception as exc:
        print(f"{tag}  FAIL {exc}")
        traceback.print_exc(limit=1)
        failed.append(d)
    if i < len(DATES):
        time.sleep(PAUSE_SECONDS)

print(f"\nFetched {len(ok)}/{len(DATES)}. Cached under data/cache/fo_YYYYMMDD.csv")
if failed:
    print("Failed (re-run this script to retry): "
          + ", ".join(f"{d:%d-%b-%Y}" for d in failed))
