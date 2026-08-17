"""
Per-stock breakdown for ONE month: baseline entry/exit vs realistic
entry/exit, to see exactly which names cost the delta.
"""
import json
import math
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "research")

import logging
logging.disable(logging.CRITICAL)

import daily_report
import harness
import strategy

y, m = int(sys.argv[1]), int(sys.argv[2])
Z80 = 0.8416212


def parkinson_sigma(row):
    h = float(row.get("high_price", 0) or 0)
    l = float(row.get("low_price", 0) or 0)
    if h <= 0 or l <= 0 or h < l:
        return None
    if h == l:
        return 0.0
    return math.log(h / l) / (2.0 * math.sqrt(math.log(2)))


ex, nx = harness.cycle_dates(y, m)
picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
merged = harness.prices(ex, nx)
after = sorted(d for d in merged if d > ex)
entry_day, day2, day3 = after[0], after[1], after[2]
hist = strategy.load_price_history(ex, harness.universe())
hist_dates = sorted(hist.keys())

rows = []
for sym in picks:
    sig_frame = hist.get(ex) if ex in hist else hist.get(hist_dates[-1])
    close = float(sig_frame.loc[sym, "close_price"])
    lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
    rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

sizing = daily_report._compute_min_portfolio_sizing(rows)
shares = sizing["shares"]
slot_target = sizing["min_portfolio"] / 10.0

entry_overrides = {}
fill_day_of = {}
day1_missed = []
for sym, s in shares.items():
    row = merged[entry_day].loc[sym]
    real_low = float(row.get("low_price", 0) or 0)
    real_open = float(row.get("open_price", 0) or 0)
    if real_low > 0 and real_low <= s["limit_price"]:
        fill_price = min(real_open, s["limit_price"]) if real_open > 0 else s["limit_price"]
        entry_overrides[sym] = (fill_price, entry_day)
        fill_day_of[sym] = 1
    else:
        day1_missed.append(sym)

day2_missed = []
for sym in day1_missed:
    row1 = merged[entry_day].loc[sym]
    sigma1 = parkinson_sigma(row1)
    day1_close = float(row1.get("close_price", 0) or 0)
    p80_2 = day1_close * math.exp(Z80 * sigma1)
    row2 = merged[day2].loc[sym]
    low2 = float(row2.get("low_price", 0) or 0)
    open2 = float(row2.get("open_price", 0) or 0)
    if low2 > 0 and low2 <= p80_2:
        fill_price2 = min(open2, p80_2) if open2 > 0 else p80_2
        entry_overrides[sym] = (fill_price2, day2)
        fill_day_of[sym] = 2
    else:
        day2_missed.append((sym, sigma1))

for sym, sigma1 in day2_missed:
    row2 = merged[day2].loc[sym]
    sigma2 = parkinson_sigma(row2)
    day2_close = float(row2.get("close_price", 0) or 0)
    row3 = merged[day3].loc[sym]
    day3_open = float(row3.get("open_price", 0) or 0)
    entry_overrides[sym] = (day3_open, day3)
    fill_day_of[sym] = 3

res_baseline = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                                 ranked_order=ranked, top_n=10, price_by_date=merged)

after_nx = [d for d in sorted(merged) if d > nx]
roll = after_nx[0] if after_nx else nx
hold = [d for d in sorted(merged) if ex < d <= roll]
res_real = strategy.simulate_month(
    ranked, merged, hold, harness.sectors(),
    basket_symbols=list(picks), top_n=10,
    stop_pct=stop_pct, target_pct=None,
    carry_forward=False, entry_overrides=entry_overrides)

# Per-symbol: baseline exit vs realistic exit
base_by_sym = {}
for slot_chain in res_baseline.slots:
    for sym, ret, reason, dt in slot_chain:
        base_by_sym[sym] = (ret, reason, dt)
# open (never exited) positions
for p in res_baseline.open_positions:
    ret = (p.last - p.entry) / p.entry * 100 if p.last else None
    base_by_sym.setdefault(p.symbol, (ret, "OPEN@close", None))

real_by_sym = {}
for slot_chain in res_real.slots:
    for sym, ret, reason, dt in slot_chain:
        real_by_sym[sym] = (ret, reason, dt)
for p in res_real.open_positions:
    ret = (p.last - p.entry) / p.entry * 100 if p.last else None
    real_by_sym.setdefault(p.symbol, (ret, "OPEN@close", None))

print(f"=== {y}-{m:02d}: baseline {res_baseline.return_pct:+.2f}% vs realistic {res_real.return_pct:+.2f}% "
     f"(delta {res_real.return_pct-res_baseline.return_pct:+.2f}pt) ===")
print()
total_delta_contrib = 0
for sym in picks:
    b = base_by_sym.get(sym)
    r = real_by_sym.get(sym)
    fday = fill_day_of.get(sym, 1)
    b_ret = b[0] if b else None
    r_ret = r[0] if r else None
    contrib = ((r_ret or 0) - (b_ret or 0)) / 10.0
    total_delta_contrib += contrib
    flag = "  <==" if abs(contrib) > 0.15 else ""
    print(f"{sym:12s} filled day{fday}  baseline: {b_ret:+.2f}% ({b[1] if b else '?'})   "
         f"realistic: {r_ret:+.2f}% ({r[1] if r else '?'})   contrib to delta: {contrib:+.3f}pt{flag}")
print()
print(f"sum of contributions: {total_delta_contrib:+.3f}pt (should ~= reported delta)")
