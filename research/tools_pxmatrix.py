"""
Build one wide price matrix for the whole cached period, once.

    python tools_pxmatrix.py          # writes /tmp/px_close.parquet etc.

`strategy.load_price_history` rebuilds a dict of per-day DataFrames on
every call, which costs ~40s for a 400-session window. Any tool that
needs several overlapping windows pays that repeatedly and blows the
shell timeout. This does the expensive pass exactly once and leaves
three date x symbol matrices on disk; slicing them afterwards is free.
"""
import datetime as dt
import logging
import os
import sys

import numpy as np
import pandas as pd

logging.disable(logging.CRITICAL)
import strategy                                             # noqa: E402

OUT = "/tmp/px_{}.parquet"
FIELDS = ("close_price", "high_price", "turnover")
NAMES = ("close", "high", "turnover")


def main():
    end = dt.date(2026, 7, 31)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 700
    uni = strategy.load_fo_universe()
    hist = strategy.load_price_history(end, uni, days=days)
    alld = sorted(hist)
    print(f"frames {len(alld)}  {alld[0]} -> {alld[-1]}", flush=True)

    for field, name in zip(FIELDS, NAMES):
        cols = {}
        for s in uni:
            vals = []
            for d in alld:
                f = hist[d]
                v = np.nan
                if s in f.index and field in f.columns:
                    x = f.at[s, field]
                    if pd.notna(x):
                        v = float(x)
                vals.append(v)
            cols[s] = vals
        df = pd.DataFrame(cols, index=pd.to_datetime(alld))
        df.to_parquet(OUT.format(name))
        print(f"  {name}: {df.shape} -> {OUT.format(name)}", flush=True)


if __name__ == "__main__":
    main()
