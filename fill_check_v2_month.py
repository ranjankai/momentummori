"""
Revised 3-day chain per the user's spec:

Day 1: unchanged -- original 20-day rolling band, standard limit + solve.
Day 2: the 20-day band is abandoned for misses (it demonstrably failed).
       Instead use Day-1's OWN realized volatility (Parkinson estimator
       off that single day's H/L) to compute an 80%-probability opening
       price for Day 2, anchored off Day-1's close. Limit order at that
       price; shares = round(slot/price).
Day 3: pool Day-1 and Day-2's realized vols (average variance) for a
       steadier 2-day estimate, compute an 80%-probability opening price
       for Day 3 anchored off Day-2's close, and set share count from
       THAT (decided the evening before, no lookahead at the real Day-3
       price). Execute at Day 3's actual market open, no limit.

Appends one JSON line to fill_results_v2.jsonl.
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
out = {"month": f"{y}-{m:02d}"}

Z80 = 0.8416212  # 80th percentile of the standard normal


def solve_one(slot, close, lo, hi):
    ideal_n = max(1, round(slot / close))
    best_local = None
    for n in {ideal_n, max(1, ideal_n - 1), ideal_n + 1}:
        price = min(max(slot / n, lo), hi)
        dev = abs(n * price - slot) / slot * 100.0
        if best_local is None or dev < best_local[2]:
            best_local = (n, price, dev)
    return best_local


def parkinson_sigma(row):
    """Single-day realized vol estimate from that day's own H/L."""
    h = float(row.get("high_price", 0) or 0)
    l = float(row.get("low_price", 0) or 0)
    if h <= 0 or l <= 0 or h < l:
        return None
    if h == l:
        return 0.0
    return math.log(h / l) / (2.0 * math.sqrt(math.log(2)))


try:
    ex, nx = harness.cycle_dates(y, m)
    picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
    merged = harness.prices(ex, nx)
    after = sorted(d for d in merged if d > ex)
    if len(after) < 3:
        out["error"] = "no day-3 data"
    else:
        entry_day, day2, day3 = after[0], after[1], after[2]
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
            lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
            rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

        if not rows:
            out["error"] = "no valid rows"
        else:
            sizing = daily_report._compute_min_portfolio_sizing(rows)
            shares = sizing["shares"]
            slot_target = sizing["min_portfolio"] / 10.0

            final = {}
            day1_missed = []
            for sym, s in shares.items():
                row = merged[entry_day].loc[sym]
                real_low = float(row.get("low_price", 0) or 0)
                if real_low > 0 and real_low <= s["limit_price"]:
                    final[sym] = {"n": s["shares"], "price": s["limit_price"],
                                  "invested": s["shares"] * s["limit_price"],
                                  "filled_day": 1}
                else:
                    day1_missed.append(sym)

            day2_detail = []
            day2_missed = []  # (sym, sigma1)
            for sym in day1_missed:
                row1 = merged[entry_day].loc[sym]
                sigma1 = parkinson_sigma(row1)
                day1_close = float(row1.get("close_price", 0) or 0)
                if sigma1 is None or day1_close <= 0:
                    day2_missed.append((sym, sigma1))
                    continue
                p80_2 = day1_close * math.exp(Z80 * sigma1)
                n2 = max(1, round(slot_target / p80_2))
                row2 = merged[day2].loc[sym]
                low2 = float(row2.get("low_price", 0) or 0)
                open2 = float(row2.get("open_price", 0) or 0)
                filled2 = low2 > 0 and low2 <= p80_2
                fill_price2 = min(open2, p80_2) if filled2 and open2 > 0 else p80_2
                day2_detail.append({"symbol": sym, "sigma1_pct": round(sigma1 * 100, 3),
                                    "p80_price": round(p80_2, 2), "shares": n2,
                                    "day2_low": round(low2, 2), "day2_open": round(open2, 2),
                                    "filled": filled2})
                if filled2:
                    final[sym] = {"n": n2, "price": fill_price2,
                                  "invested": n2 * fill_price2, "filled_day": 2}
                else:
                    day2_missed.append((sym, sigma1))

            day3_detail = []
            for sym, sigma1 in day2_missed:
                row2 = merged[day2].loc[sym]
                sigma2 = parkinson_sigma(row2)
                day2_close = float(row2.get("close_price", 0) or 0)
                if sigma1 is None or sigma2 is None or day2_close <= 0:
                    continue
                sigma_pooled = math.sqrt((sigma1 ** 2 + sigma2 ** 2) / 2.0)
                p80_3 = day2_close * math.exp(Z80 * sigma_pooled)
                n3 = max(1, round(slot_target / p80_3))  # decided in advance
                row3 = merged[day3].loc[sym]
                day3_open = float(row3.get("open_price", 0) or 0)
                invested3 = n3 * day3_open
                dev3 = abs(invested3 - slot_target) / slot_target * 100.0
                day3_detail.append({
                    "symbol": sym, "sigma_pooled_pct": round(sigma_pooled * 100, 3),
                    "p80_price": round(p80_3, 2), "shares_preplanned": n3,
                    "day3_actual_open": round(day3_open, 2),
                    "weight_dev_pct": round(dev3, 2),
                })
                final[sym] = {"n": n3, "price": day3_open, "invested": invested3,
                              "filled_day": 3}

            all_dev = [{"symbol": sym, "dev_pct": round(abs(v["invested"] - slot_target)
                                                        / slot_target * 100.0, 2),
                       "filled_day": v["filled_day"]}
                      for sym, v in final.items()]

            out.update({
                "entry_day": str(entry_day), "day2": str(day2), "day3": str(day3),
                "slot_target": round(slot_target, 2),
                "day2_detail": day2_detail,
                "day3_detail": day3_detail,
                "all_deviations": all_dev,
            })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

with open("fill_results_v2.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps(out))
