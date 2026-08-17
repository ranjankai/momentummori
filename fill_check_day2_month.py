"""
Day-2 fill check for ONE cycle. Reuses day-1 logic to find the misses, then
re-quotes each missed name off day-1's close (same band mechanism, fresh
anchor) and checks the REAL day-2 low against that new limit. Slot size is
held fixed at whatever day-1's sizing committed to.

Appends one JSON line to fill_results_day2.jsonl.
"""
import datetime as dt
import json
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "research")

import logging
logging.disable(logging.CRITICAL)

import daily_report
import harness
import strategy

y, m = int(sys.argv[1]), int(sys.argv[2])
out = {"month": f"{y}-{m:02d}"}


def solve_one(slot, close, lo, hi):
    ideal_n = max(1, round(slot / close))
    best_local = None
    for n in {ideal_n, max(1, ideal_n - 1), ideal_n + 1}:
        price = min(max(slot / n, lo), hi)
        dev = abs(n * price - slot) / slot * 100.0
        if best_local is None or dev < best_local[2]:
            best_local = (n, price, dev)
    return best_local


try:
    ex, nx = harness.cycle_dates(y, m)
    picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
    merged = harness.prices(ex, nx)
    after = sorted(d for d in merged if d > ex)
    if len(after) < 2:
        out["error"] = "no day-2 data"
    else:
        entry_day, day2 = after[0], after[1]
        hist = strategy.load_price_history(ex, harness.universe())
        hist_dates = sorted(hist.keys())

        rows = []
        for sym in picks:
            if entry_day not in merged or sym not in merged[entry_day].index:
                continue
            sig_frame = hist.get(ex) if ex in hist else hist.get(hist_dates[-1])
            if sig_frame is None or sym not in sig_frame.index:
                continue
            close = float(sig_frame.loc[sym, "close_price"])
            if close <= 0:
                continue
            lo, hi, band_pct = daily_report._compute_stock_entry_band(sym, hist, close)
            rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

        if not rows:
            out["error"] = "no valid rows"
        else:
            sizing = daily_report._compute_min_portfolio_sizing(rows)
            shares = sizing["shares"]
            slot_target = sizing["min_portfolio"] / 10.0

            day1_missed = []
            for sym, s in shares.items():
                limit_price = s["limit_price"]
                row = merged[entry_day].loc[sym]
                real_low = float(row.get("low_price", 0) or 0)
                if not (real_low > 0 and real_low <= limit_price):
                    day1_missed.append(sym)

            # Requote each day-1 miss off entry_day's close, check day-2 low.
            hist2 = dict(hist)
            hist2[entry_day] = merged[entry_day]

            still_missed = []
            recovered = []
            for sym in day1_missed:
                if sym not in merged[entry_day].index or sym not in merged[day2].index:
                    still_missed.append({"symbol": sym, "reason": "no day2 data"})
                    continue
                new_close = float(merged[entry_day].loc[sym, "close_price"])
                if new_close <= 0:
                    still_missed.append({"symbol": sym, "reason": "bad close"})
                    continue
                lo2, hi2, _ = daily_report._compute_stock_entry_band(sym, hist2, new_close)
                n2, price2, dev2 = solve_one(slot_target, new_close, lo2, hi2)
                row2 = merged[day2].loc[sym]
                low2 = float(row2.get("low_price", 0) or 0)
                open2 = float(row2.get("open_price", 0) or 0)
                filled2 = low2 > 0 and low2 <= price2
                rec = {"symbol": sym, "requoted_limit": round(price2, 2),
                       "day2_low": round(low2, 2), "day2_open": round(open2, 2)}
                if filled2:
                    recovered.append(rec)
                else:
                    still_missed.append(rec)

            out.update({
                "entry_day": str(entry_day), "day2": str(day2),
                "n_day1_missed": len(day1_missed),
                "n_recovered_day2": len(recovered),
                "n_still_missed_day2": len(still_missed),
                "recovered": recovered,
                "still_missed": still_missed,
            })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

with open("fill_results_day2.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps(out))
