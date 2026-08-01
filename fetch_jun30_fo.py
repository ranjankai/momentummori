"""
One-off: fetch and cache the F&O bhavcopy for 30-Jun-2026 (June monthly
expiry -- last Tuesday of June 2026). Needed for the July basket
selection: rollover % and cost of carry both come from this file.

Run from the momentum-tracker project root:

    python fetch_jun30_fo.py

Then verify the TradDt column reads 2026-06-30.
"""
import sys
sys.path.insert(0, ".")
from datetime import date

import nse_client

d = date(2026, 6, 30)
df = nse_client.fetch_fo_bhavcopy(d)
print(f"Fetched {len(df)} rows for {d}. Cached at data/cache/fo_{d:%Y%m%d}.csv")
if "TradDt" in df.columns:
    print("TradDt values in file:", df["TradDt"].unique())
