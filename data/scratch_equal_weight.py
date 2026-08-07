"""
Compute the minimum portfolio size for the July basket where equal-weighting
stays within 10% deviation, using realistic entry ranges derived from each
stock's recent daily range (not a fixed ±2%).

Approach:
  1. Pull 20 sessions of OHLC for each stock around the expiry date
  2. Compute typical daily range as median((high - low) / close)
  3. Entry range = close * (1 ± daily_range_pct)
  4. Worst case for sizing: most expensive stock at its HIGH, cheapest at LOW
  5. Find minimum portfolio where max weight deviation <= 10%
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import nse_client, scoring
import pandas as pd
import numpy as np

EXPIRY = date(2026, 7, 28)
BASKET = [
    "POWERINDIA", "FORCEMOT", "AMBER", "GVT&D", "KAYNES",
    "TRENT", "ADANIGREEN", "BANDHANBNK", "SAIL", "IDEA",
]

# --- 1. Pull 20 sessions of OHLC around the expiry ---
print("Loading recent OHLC...")
ohlc = {sym: [] for sym in BASKET}
d = EXPIRY
sessions = 0
while sessions < 20:
    d -= timedelta(days=1)
    if d.weekday() >= 5:
        continue
    try:
        raw = nse_client.fetch_cm_bhavcopy(d)
    except nse_client.NseFetchError:
        continue
    sessions += 1
    for sym in BASKET:
        rows = raw[raw["TckrSymb"] == sym]
        if rows.empty:
            continue
        row = rows.iloc[0]
        o = float(row.get("OpnPric", 0))
        h = float(row.get("HghPric", 0))
        l = float(row.get("LwPric", 0))
        c = float(row.get("ClsPric", 0))
        if h > 0 and l > 0 and c > 0:
            ohlc[sym].append({"date": d, "open": o, "high": h, "low": l, "close": c})

# --- 2. Compute entry range per stock ---
print()
print(f"{'Stock':<14} {'Close':>10} {'DayRange%':>10} {'Entry Lo':>10} {'Entry Hi':>10}")
print("-" * 58)

entry_ranges = {}
for sym in BASKET:
    df = pd.DataFrame(ohlc[sym])
    if df.empty:
        print(f"{sym:<14}  NO DATA")
        continue
    # Expiry close is the reference price
    close = df.sort_values("date").iloc[-1]["close"]
    # Median daily range as % of close
    df["range_pct"] = (df["high"] - df["low"]) / df["close"]
    median_range = df["range_pct"].median()
    # Also compute open-to-close deviation (how far does open drift from prev close?)
    df_sorted = df.sort_values("date")
    df_sorted["prev_close"] = df_sorted["close"].shift(1)
    open_dev = ((df_sorted["open"] - df_sorted["prev_close"]) / df_sorted["prev_close"]).dropna().abs()
    gap_pct = open_dev.median()

    # Entry range: use the larger of daily range or gap as the band
    band = max(median_range, gap_pct)
    lo = close * (1 - band)
    hi = close * (1 + band)
    entry_ranges[sym] = {"close": close, "band_pct": band * 100, "lo": lo, "hi": hi}
    print(f"{sym:<14} {close:>10,.2f} {band*100:>9.2f}% {lo:>10,.2f} {hi:>10,.2f}")

# --- 3. Find minimum portfolio at 10% weight tolerance ---
# Worst case: most expensive at HIGH, compute shares at those prices
print()
print("=" * 58)
print("Finding minimum portfolio (max weight deviation <= 10%)...")
print("=" * 58)

MAX_DEV = 0.10
best = None

for portfolio in range(100000, 5000001, 500):
    slot = portfolio / 10
    ok = True
    max_dev = 0
    details = {}
    for sym, r in entry_ranges.items():
        # Worst case: buy at the HIGH end of the range
        px = r["hi"]
        shares = round(slot / px)
        if shares < 1:
            shares = 1
        actual = shares * px
        dev = abs(actual - slot) / slot
        max_dev = max(max_dev, dev)
        if dev > MAX_DEV:
            ok = False
        details[sym] = (shares, actual, dev, px)
    if ok and (best is None or portfolio < best[0]):
        best = (portfolio, details, max_dev)
        break  # first valid is the minimum

if best:
    portfolio, details, max_dev = best
    slot = portfolio / 10
    print(f"\nMINIMUM PORTFOLIO: Rs {portfolio:,}")
    print(f"Per slot: Rs {slot:,.0f}")
    print(f"Max deviation: {max_dev*100:.1f}%")
    print()
    hdr = f"{'Stock':<14} {'EntryHi':>10} {'Shares':>7} {'Invested':>12} {'Wt%':>6} {'Dev%':>7}"
    print(hdr)
    print("-" * len(hdr))
    total = 0
    for sym in sorted(details, key=lambda s: -entry_ranges[s]["hi"]):
        shares, actual, dev, px = details[sym]
        wt = actual / portfolio * 100
        total += actual
        sign = "+" if actual >= slot else "-"
        print(f"{sym:<14} {px:>10,.2f} {shares:>7,} {actual:>12,.2f} {wt:>5.1f}% {sign}{dev*100:>5.1f}%")
    print()
    print(f"Total invested: Rs {total:,.0f}")
    print(f"Cash remainder: Rs {portfolio - total:,.0f}")
else:
    print("No solution found under Rs 50 lakh")
