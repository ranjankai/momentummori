"""
One-off: fetch and cache the F&O bhavcopy for 24-Feb-2026 (this was
missing from data/cache/ -- only 27-Feb was cached). Run from the
momentum-tracker project root:

    python fetch_feb24_fo.py
"""
import sys
sys.path.insert(0, ".")
from datetime import date

import nse_client

d = date(2026, 2, 24)
df = nse_client.fetch_fo_bhavcopy(d)
print(f"Fetched {len(df)} rows for {d}. Cached at data/cache/fo_{d:%Y%m%d}.csv")
