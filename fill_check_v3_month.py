"""
v3: adds the two fixes just agreed on top of the v2 fill chain.

1) Stop/target are ALWAYS anchored to Day-1's actual market open (the
   "arrival price" -- Perold's decision-price concept), never to
   whatever price the delayed fill actually happened at. This stops a
   late, higher entry from inheriting a stop that's crept closer to the
   market purely because the fill was late.

2) Before attempting Day 2 or Day 3, check whether the PRECEDING day's
   low already breached the anchor-based stop (day1_open * (1-stop%)).
   If it has -- or if Day 3's own actual open has already gapped
   through it -- ABORT: do not enter this name at all this month, slot
   stays in cash. Never buy something that's already through its own
   risk boundary before you own it (gap risk).

Simulates returns both ways (baseline vs v3-realistic) through the same
strategy.simulate_month. Appends one JSON line to fill_results_v3.jsonl.
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

            entry_overrides = {}
            aborted = []
            day1_missed = []
            anchor_of = {}

            for sym, s in shares.items():
                row1 = merged[entry_day].loc[sym]
                day1_open = float(row1.get("open_price", 0) or 0)
                anchor_of[sym] = day1_open
                real_low = float(row1.get("low_price", 0) or 0)
                if real_low > 0 and real_low <= s["limit_price"]:
                    fill_price = min(day1_open, s["limit_price"]) if day1_open > 0 else s["limit_price"]
                    entry_overrides[sym] = (fill_price, entry_day, day1_open)
                else:
                    day1_missed.append(sym)

            day2_missed = []
            for sym in day1_missed:
                anchor = anchor_of[sym]
                anchor_stop = anchor * (1 - stop_pct / 100.0)
                row1 = merged[entry_day].loc[sym]
                low1 = float(row1.get("low_price", 0) or 0)
                if low1 > 0 and low1 <= anchor_stop:
                    entry_overrides[sym] = None
                    aborted.append({"symbol": sym, "stage": "before day2",
                                    "anchor_stop": round(anchor_stop, 2),
                                    "trigger_low": round(low1, 2)})
                    continue

                sigma1 = parkinson_sigma(row1)
                day1_close = float(row1.get("close_price", 0) or 0)
                if sigma1 is None or day1_close <= 0:
                    day2_missed.append((sym, sigma1))
                    continue
                p80_2 = day1_close * math.exp(Z80 * sigma1)
                row2 = merged[day2].loc[sym]
                low2 = float(row2.get("low_price", 0) or 0)
                open2 = float(row2.get("open_price", 0) or 0)
                if low2 > 0 and low2 <= p80_2:
                    fill_price2 = min(open2, p80_2) if open2 > 0 else p80_2
                    entry_overrides[sym] = (fill_price2, day2, anchor)
                else:
                    day2_missed.append((sym, sigma1))

            for sym, sigma1 in day2_missed:
                anchor = anchor_of[sym]
                anchor_stop = anchor * (1 - stop_pct / 100.0)
                row2 = merged[day2].loc[sym]
                low2 = float(row2.get("low_price", 0) or 0)
                if low2 > 0 and low2 <= anchor_stop:
                    entry_overrides[sym] = None
                    aborted.append({"symbol": sym, "stage": "before day3",
                                    "anchor_stop": round(anchor_stop, 2),
                                    "trigger_low": round(low2, 2)})
                    continue

                row3 = merged[day3].loc[sym]
                day3_open = float(row3.get("open_price", 0) or 0)
                if day3_open > 0 and day3_open <= anchor_stop:
                    entry_overrides[sym] = None
                    aborted.append({"symbol": sym, "stage": "day3 open itself",
                                    "anchor_stop": round(anchor_stop, 2),
                                    "trigger_open": round(day3_open, 2)})
                    continue

                entry_overrides[sym] = (day3_open, day3, anchor)

            res_baseline = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                                             ranked_order=list(picks), top_n=10,
                                             price_by_date=merged)

            after_nx = [d for d in sorted(merged) if d > nx]
            roll = after_nx[0] if after_nx else nx
            hold = [d for d in sorted(merged) if ex < d <= roll]
            res_real = strategy.simulate_month(
                list(picks), merged, hold, harness.sectors(),
                basket_symbols=list(picks), top_n=10,
                stop_pct=stop_pct, target_pct=None,
                carry_forward=False, entry_overrides=entry_overrides)

            out.update({
                "baseline_ret": round(res_baseline.return_pct, 3),
                "realistic_ret": round(res_real.return_pct, 3),
                "delta_pts": round(res_real.return_pct - res_baseline.return_pct, 3),
                "n_aborted": len(aborted),
                "aborted": aborted,
                "n_filled": sum(1 for v in entry_overrides.values() if v is not None),
            })
except Exception as exc:
    import traceback
    out["error"] = f"{type(exc).__name__}: {exc}"
    out["trace"] = traceback.format_exc()[-1500:]

with open("fill_results_v3.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps({k: v for k, v in out.items() if k != "trace"}))
if "trace" in out:
    print(out["trace"])
