"""
Day-1 fill check: for each historical cycle, compute the entry sheet exactly
as daily_report.build_entry_sheet would (same functions, no forking), then
check the REAL next-session low against the quoted limit price to see
whether each name would actually have filled on day 1.

Uses only cached bhavcopy already on disk -- no network calls.
"""
import datetime as dt
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "research")

import logging
logging.disable(logging.CRITICAL)

import daily_report
import harness
import strategy

MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2),
          (2026, 3), (2026, 4), (2026, 5), (2026, 6)]

sectors = harness.sectors()

total_names = 0
total_missed = 0
rows_out = []

for y, m in MONTHS:
    try:
        ex, nx = harness.cycle_dates(y, m)
        picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
        merged = harness.prices(ex, nx)
        after = sorted(d for d in merged if d > ex)
        if not after:
            print(f"{y}-{m:02d}: no entry-day data, skipping")
            continue
        entry_day = after[0]
        hist = strategy.load_price_history(ex, harness.universe())

        rows = []
        for sym in picks:
            if entry_day not in merged or sym not in merged[entry_day].index:
                continue
            # close on the SIGNAL day (ex) is what the band is built from
            hist_dates = sorted(hist.keys())
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
            print(f"{y}-{m:02d}: no valid rows, skipping")
            continue

        sizing = daily_report._compute_min_portfolio_sizing(rows)
        shares = sizing["shares"]

        missed_this_month = []
        for sym, s in shares.items():
            limit_price = s["limit_price"]
            row = merged[entry_day].loc[sym]
            real_low = float(row.get("low_price", 0) or 0)
            real_open = float(row.get("open_price", 0) or 0)
            total_names += 1
            filled = real_low > 0 and real_low <= limit_price
            if not filled:
                total_missed += 1
                missed_this_month.append(
                    f"{sym}(limit {limit_price:.1f} vs low {real_low:.1f}, "
                    f"open {real_open:.1f})")

        rows_out.append((f"{y}-{m:02d}", len(shares), len(missed_this_month),
                         sizing["min_portfolio"], missed_this_month))
        print(f"{y}-{m:02d}: {len(missed_this_month)}/{len(shares)} missed on day 1 "
              f"(min portfolio Rs {sizing['min_portfolio']:,})")
        for x in missed_this_month:
            print(f"    {x}")

    except Exception as exc:
        print(f"{y}-{m:02d}: ERROR {type(exc).__name__}: {exc}")

print()
print(f"TOTAL: {total_missed}/{total_names} name-months missed a day-1 fill "
      f"({total_missed/total_names*100:.1f}%)" if total_names else "no data")
