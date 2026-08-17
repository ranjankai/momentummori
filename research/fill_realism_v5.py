"""
V5 = V4's basket (picks, sector caps, regime stop) + "basket actualization":
a realistic multi-day entry mechanism instead of the V4 backtest's
perfect-same-day-open assumption. Delegates position walking to
strategy.simulate_month throughout -- do not fork the loop.

MECHANISM (revised 13-Aug-2026 -- simplified from 3 stages to 2 to match
the agreed investor message cadence: Day0 quote / Day1-eve update / Day2-eve
final fill list / Day3-eve repeat / Day4 normal service. See
BACKTEST_LOG.md V5 section and its 13-Aug-2026 addendum.)
----------------------------------------------------------------
Day 1: standard 20-day volatility band (daily_report._compute_stock_entry_
       band), whole-share sizing solved against the priciest stock's own
       band low x10 slots (daily_report._compute_min_portfolio_sizing).
       Real fill if that day's low <= the quoted limit.
Day 2 (day-1 misses only): MANDATORY, unconditional market buy -- no limit,
       the basket must be complete by end of Day 2. Share count is decided
       the evening before (Day-1 close, no lookahead) from an 80%-
       probability price estimate off Day-1's OWN realized volatility
       (Parkinson estimator off that single day's H/L, anchored off
       Day-1's close) -- used only to size the position, since Day 2 fills
       at whatever the actual open is, not at that estimate.

RISK ANCHOR (the 12-Aug-2026 fix, unchanged)
----------------------------------
Stop and target are ALWAYS computed off Day-1's actual market open --
the "arrival price" / decision price in Perold's (1988) Implementation
Shortfall framework -- never off wherever the delayed fill actually
happened. Anchoring to a late, higher fill inflates the stop toward the
market purely as an artefact of the entry being late; this is what
caused the PNBHOUSING (-1.34pt) and BSE (-2.21pt) blowouts in the
pre-fix backtest (both flipped from big winners to stopped-out losses
because their stop crept up with a delayed entry).

GAP-RISK ABORT
--------------
Before the Day-2 mandatory buy executes, Day-2's own open is checked
against the anchor-based stop (Day-1's own low is checked the same way
before ever quoting a Day-2 plan). If price has already gapped through
it, the entry is ABORTED -- the slot stays in cash for the month, no
fallback fill. Never buy something that's already through its own risk
boundary before you own it (a well-documented failure mode: gap risk
bypasses ordinary stop protection entirely).

OUTPUT
------
One JSON line per month to data/fill_realism_v5.jsonl, containing the
per-stock fill detail (day filled, price, shares, weight, deviation from
10%) and the return comparison (baseline vs V5) for that cycle.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import daily_report
import harness
import strategy
import config

Z80 = 0.8416212  # 80th percentile, standard normal
OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "fill_realism_v5.jsonl")


def parkinson_sigma(row):
    h = float(row.get("high_price", 0) or 0)
    l = float(row.get("low_price", 0) or 0)
    if h <= 0 or l <= 0 or h < l:
        return None
    if h == l:
        return 0.0
    return math.log(h / l) / (2.0 * math.sqrt(math.log(2)))


def run_month(y, m):
    out = {"month": f"{y}-{m:02d}"}
    try:
        ex, nx = harness.cycle_dates(y, m)
        picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
        merged = harness.prices(ex, nx)
        after = sorted(d for d in merged if d > ex)
        if len(after) < 2:
            out["error"] = "no day-2 data"
            return out

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
            lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close)
            rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})

        if not rows:
            out["error"] = "no valid rows"
            return out

        sizing = daily_report._compute_min_portfolio_sizing(rows)
        shares = sizing["shares"]
        # The unrounded field directly -- not min_portfolio/10, which
        # divides an ALREADY-ROUNDED total and can drift from the true
        # slot_target by a rupee-level residue (14-Aug-2026 audit finding).
        slot_target = sizing["slot_target"]

        entry_overrides = {}
        stock_detail = {}
        anchor_of = {}
        day1_missed = []

        for sym, s in shares.items():
            row1 = merged[entry_day].loc[sym]
            day1_open = float(row1.get("open_price", 0) or 0)
            if day1_open <= 0:
                # No usable open -- can't establish an anchor for this name
                # this cycle; treat like a data gap (aborted, no fallback).
                entry_overrides[sym] = None
                stock_detail[sym] = {"filled_day": None, "aborted_stage": "no_day1_open"}
                continue
            anchor_of[sym] = day1_open
            real_low = float(row1.get("low_price", 0) or 0)
            if real_low > 0 and real_low <= s["limit_price"]:
                fill_price = min(day1_open, s["limit_price"])
                entry_overrides[sym] = (fill_price, entry_day, day1_open)
                stock_detail[sym] = {"filled_day": 1, "price": fill_price,
                                     "shares": s["shares"], "risk_anchor": day1_open}
            else:
                day1_missed.append(sym)

        for sym in day1_missed:
            anchor = anchor_of[sym]
            anchor_stop = anchor * (1 - stop_pct / 100.0)
            row1 = merged[entry_day].loc[sym]
            low1 = float(row1.get("low_price", 0) or 0)
            if low1 > 0 and low1 <= anchor_stop:
                entry_overrides[sym] = None
                stock_detail[sym] = {"filled_day": None, "aborted_stage": "before_day2",
                                     "anchor_stop": round(anchor_stop, 2),
                                     "trigger_low": round(low1, 2)}
                continue

            sigma1 = parkinson_sigma(row1)
            day1_close = float(row1.get("close_price", 0) or 0)
            if sigma1 is None or day1_close <= 0:
                stock_detail[sym] = {"filled_day": None, "aborted_stage": "no_vol_data"}
                entry_overrides[sym] = None
                continue
            p80_2 = day1_close * math.exp(Z80 * sigma1)
            n2 = max(1, round(slot_target / p80_2))  # decided the evening before

            if day2 not in merged or sym not in merged[day2].index:
                stock_detail[sym] = {"filled_day": None, "aborted_stage": "unresolved"}
                entry_overrides[sym] = None
                continue
            row2 = merged[day2].loc[sym]
            open2 = float(row2.get("open_price", 0) or 0)
            if open2 <= 0 or open2 <= anchor_stop:
                entry_overrides[sym] = None
                stock_detail[sym] = {"filled_day": None, "aborted_stage": "day2_open_itself",
                                     "anchor_stop": round(anchor_stop, 2),
                                     "trigger_open": round(open2, 2)}
                continue

            # Mandatory: fills at Day-2's actual open, no limit.
            entry_overrides[sym] = (open2, day2, anchor)
            stock_detail[sym] = {"filled_day": 2, "price": open2,
                                 "shares": n2, "risk_anchor": anchor,
                                 "p80_price": round(p80_2, 2)}

        total_invested = sum(d["shares"] * d["price"] for d in stock_detail.values()
                             if d.get("filled_day") is not None)
        for sym, d in stock_detail.items():
            if d.get("filled_day") is not None:
                invested = d["shares"] * d["price"]
                weight = (invested / total_invested * 100.0) if total_invested else 0.0
                d["invested"] = round(invested, 2)
                d["weight_pct"] = round(weight, 3)
                d["dev_from_10pct"] = round(weight - 10.0, 3)

        # Returns: baseline (perfect execution, no overrides) vs V5 (this chain).
        res_baseline = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                                         ranked_order=list(picks), top_n=10,
                                         price_by_date=merged)
        after_nx = [d for d in sorted(merged) if d > nx]
        roll = after_nx[0] if after_nx else nx
        hold = [d for d in sorted(merged) if ex < d <= roll]
        res_v5 = strategy.simulate_month(
            list(picks), merged, hold, harness.sectors(),
            basket_symbols=list(picks), top_n=10,
            stop_pct=stop_pct, target_pct=None,
            carry_forward=False, entry_overrides=entry_overrides)

        n_aborted = sum(1 for d in stock_detail.values() if d.get("filled_day") is None)
        out.update({
            "expiry": str(ex), "entry_day": str(entry_day), "day2": str(day2),
            "stop_pct": stop_pct, "breadth": round(breadth, 2),
            "slot_target": round(slot_target, 2),
            "min_portfolio": sizing["min_portfolio"],
            "n_aborted": n_aborted,
            "n_filled_day1": sum(1 for d in stock_detail.values() if d.get("filled_day") == 1),
            "n_filled_day2": sum(1 for d in stock_detail.values() if d.get("filled_day") == 2),
            "stocks": stock_detail,
            "baseline_return_pct": round(res_baseline.return_pct, 3),
            "v5_return_pct": round(res_v5.return_pct, 3),
            "delta_pts": round(res_v5.return_pct - res_baseline.return_pct, 3),
        })
    except Exception as exc:
        import traceback
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-1500:]
    return out


if __name__ == "__main__":
    y, m = int(sys.argv[1]), int(sys.argv[2])
    result = run_month(y, m)
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "a") as fh:
        fh.write(json.dumps(result) + "\n")
    printable = {k: v for k, v in result.items() if k not in ("trace", "stocks")}
    print(json.dumps(printable))
    if "trace" in result:
        print(result["trace"])
