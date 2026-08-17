"""
Day-1 fill check for ONE cycle (year month passed as argv), appended as one
JSON line to fill_results.jsonl. Run once per month across separate calls
so no single call exceeds the sandbox's execution time cap.
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

try:
    ex, nx = harness.cycle_dates(y, m)
    picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
    merged = harness.prices(ex, nx)
    after = sorted(d for d in merged if d > ex)
    if not after:
        out["error"] = "no entry-day data"
    else:
        entry_day = after[0]
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
            rows.append({"symbol": sym, "close": close,
                         "entry_lo": lo, "entry_hi": hi})

        if not rows:
            out["error"] = "no valid rows"
        else:
            sizing = daily_report._compute_min_portfolio_sizing(rows)
            shares = sizing["shares"]
            missed = []
            names = []
            for sym, s in shares.items():
                limit_price = s["limit_price"]
                row = merged[entry_day].loc[sym]
                real_low = float(row.get("low_price", 0) or 0)
                real_open = float(row.get("open_price", 0) or 0)
                names.append(sym)
                filled = real_low > 0 and real_low <= limit_price
                if not filled:
                    missed.append({"symbol": sym, "limit": round(limit_price, 2),
                                   "low": round(real_low, 2), "open": round(real_open, 2)})
            out.update({
                "entry_day": str(entry_day),
                "n_names": len(names),
                "n_missed": len(missed),
                "min_portfolio": sizing["min_portfolio"],
                "missed": missed,
            })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

with open("fill_results.jsonl", "a") as fh:
    fh.write(json.dumps(out) + "\n")
print(json.dumps(out))
