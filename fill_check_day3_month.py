"""
Full 3-day chain for ONE cycle: day1 limit -> day2 requote -> day3 FORCED
market buy (at day3's actual open) for anything still unfilled. Reports,
per forced name, the delta vs. what a plain market order on day 1's open
would have cost, and the resulting weight deviation from the equal-weight
slot given the share count already committed.

Appends one JSON line to fill_results_day3.jsonl.
"""
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

            final = {}  # symbol -> {n, invested, filled_day}
            day1_missed = []
            for sym, s in shares.items():
                row = merged[entry_day].loc[sym]
                real_low = float(row.get("low_price", 0) or 0)
                if real_low > 0 and real_low <= s["limit_price"]:
                    final[sym] = {"n": s["shares"], "price": s["limit_price"],
                                  "invested": s["shares"] * s["limit_price"],
                                  "filled_day": 1}
                else:
                    day1_missed.append((sym, float(row.get("open_price", 0) or 0)))

            hist2 = dict(hist)
            hist2[entry_day] = merged[entry_day]

            day2_missed = []  # (sym, day1_open, n2, price2)
            for sym, day1_open in day1_missed:
                new_close = float(merged[entry_day].loc[sym, "close_price"])
                lo2, hi2, _ = daily_report._compute_stock_entry_band(sym, hist2, new_close)
                n2, price2, _ = solve_one(slot_target, new_close, lo2, hi2)
                row2 = merged[day2].loc[sym]
                low2 = float(row2.get("low_price", 0) or 0)
                if low2 > 0 and low2 <= price2:
                    final[sym] = {"n": n2, "price": price2, "invested": n2 * price2,
                                  "filled_day": 2}
                else:
                    day2_missed.append((sym, day1_open, n2, price2))

            forced = []
            for sym, day1_open, n2_stale, price2 in day2_missed:
                row3 = merged[day3].loc[sym]
                day3_open = float(row3.get("open_price", 0) or 0)
                # No band left to solve within -- it's a market fill, price
                # is whatever it is. Re-resolve share count fresh against
                # that realized price instead of carrying n2 forward stale;
                # nearest-integer rounding is the exact minimizer of
                # |n*price - slot| once price is fixed, so no band search
                # is needed here the way there was on day 1/2.
                n3 = max(1, round(slot_target / day3_open)) if day3_open > 0 else n2_stale
                invested = n3 * day3_open
                dev = abs(invested - slot_target) / slot_target * 100.0
                delta_vs_day1_open = ((day3_open - day1_open) / day1_open * 100.0
                                      if day1_open > 0 else None)
                final[sym] = {"n": n3, "price": day3_open, "invested": invested,
                              "filled_day": 3}
                forced.append({
                    "symbol": sym, "shares_stale_n2": n2_stale, "shares_resolved": n3,
                    "day1_market_open": round(day1_open, 2),
                    "day3_forced_price": round(day3_open, 2),
                    "delta_vs_day1_open_pct": round(delta_vs_day1_open, 2)
                                              if delta_vs_day1_open is not None else None,
                    "slot_target": round(slot_target, 2),
                    "invested": round(invested, 2),
                    "weight_dev_pct": round(dev, 2),
                })

            all_dev = [{"symbol": sym, "dev_pct": round(abs(v["invested"] - slot_target)
                                                        / slot_target * 100.0, 2),
                       "filled_day": v["filled_day"]}
                      for sym, v in final.items()]

            out.update({
                "entry_day": str(entry_day), "day2": str(day2), "day3": str(day3),
                "slot_target": round(slot_target, 2),
                "n_forced_day3": len(forced),
                "forced": forced,
                "all_deviations": all_dev,
            })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

with open("fill_results_day3_resolved.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps(out))
