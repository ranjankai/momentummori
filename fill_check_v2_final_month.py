"""
Final portfolio weights after the full Day1->Day2->Day3 chain, using REAL
execution economics throughout (fill at the better of open/limit when the
order fills, not the planned price) -- so this measures what actually
happened, not what was planned.

Day 1: original 20-day band, solve()'d shares, real fill = min(open, limit)
        when day's low <= limit.
Day 2 (day-1 misses): 80%-probability price off Day-1's own realized vol,
        solve()'d shares against that price, real fill = min(open, p80)
        when day's low <= p80.
Day 3 (day-2 misses): 80%-probability price off pooled Day1+Day2 vol,
        shares decided the evening before (no lookahead), executed at
        Day 3's actual market open, no limit.

For each month, computes each stock's REALIZED weight = invested_i /
total_invested_month * 100, and its deviation from the 10% target.
Appends one JSON line to fill_results_v2_final.jsonl.
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

Z80 = 0.8416212


def parkinson_sigma(row):
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
                real_open = float(row.get("open_price", 0) or 0)
                if real_low > 0 and real_low <= s["limit_price"]:
                    fill_price = min(real_open, s["limit_price"]) if real_open > 0 else s["limit_price"]
                    final[sym] = {"n": s["shares"], "price": fill_price,
                                  "invested": s["shares"] * fill_price, "filled_day": 1}
                else:
                    day1_missed.append(sym)

            day2_missed = []
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
                if low2 > 0 and low2 <= p80_2:
                    fill_price2 = min(open2, p80_2) if open2 > 0 else p80_2
                    final[sym] = {"n": n2, "price": fill_price2,
                                  "invested": n2 * fill_price2, "filled_day": 2}
                else:
                    day2_missed.append((sym, sigma1))

            for sym, sigma1 in day2_missed:
                row2 = merged[day2].loc[sym]
                sigma2 = parkinson_sigma(row2)
                day2_close = float(row2.get("close_price", 0) or 0)
                if sigma1 is None or sigma2 is None or day2_close <= 0:
                    continue
                sigma_pooled = math.sqrt((sigma1 ** 2 + sigma2 ** 2) / 2.0)
                p80_3 = day2_close * math.exp(Z80 * sigma_pooled)
                n3 = max(1, round(slot_target / p80_3))
                row3 = merged[day3].loc[sym]
                day3_open = float(row3.get("open_price", 0) or 0)
                final[sym] = {"n": n3, "price": day3_open,
                              "invested": n3 * day3_open, "filled_day": 3}

            total_invested = sum(v["invested"] for v in final.values())
            stock_weights = []
            for sym, v in final.items():
                weight_pct = (v["invested"] / total_invested * 100.0) if total_invested else 0.0
                stock_weights.append({
                    "symbol": sym, "filled_day": v["filled_day"],
                    "shares": v["n"], "price": round(v["price"], 2),
                    "invested": round(v["invested"], 2),
                    "weight_pct": round(weight_pct, 3),
                    "dev_from_10pct": round(weight_pct - 10.0, 3),
                })

            out.update({
                "entry_day": str(entry_day), "day2": str(day2), "day3": str(day3),
                "slot_target": round(slot_target, 2),
                "total_invested": round(total_invested, 2),
                "n_names": len(final),
                "stock_weights": stock_weights,
            })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

with open("fill_results_v2_final.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps(out))
