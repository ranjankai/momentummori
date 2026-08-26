"""
25-Aug-2026: fill_realism_v5.py (section 12) models the 2-stage chain that
was live for exactly one day (13-Aug); production reverted to the ORIGINAL
3-stage chain on 14-Aug and has stayed there since (entry_tracking.py's
docstring: "MESSAGE CADENCE (3-stage, restored 14-Aug-2026)"). This script
is the 3-stage chain, rebuilt to match entry_tracking.py's CURRENT, live
formulas exactly (Z80=0.8416212, Parkinson sigma, Day-3 pools Day-1+Day-2
sigma anchored off Day-2's own close, risk_anchor = Day-1's actual open,
gap-abort before Day-2/Day-3/Day-3's-own-open) -- not a re-derivation, a
direct port of advance()'s n==1/n==2/n==3 branches in entry_tracking.py.

Also, unlike the old fill_realism_v5_3stage_backup.jsonl (generated
12-Aug-2026), Day-1's sizing here calls TODAY's daily_report.
_compute_min_portfolio_sizing -- which as of tonight (25-Aug-2026) no
longer lets a whole-share fit land above the stock's own last close (the
_solve_shares_to_slot tie-break + close-cap fix). A lower Day-1 limit is a
STRICTER fill condition (day's low has to reach further down), so this can
only move names toward Day-2/Day-3 relative to the old backup, never the
other way -- this run is what tells us by how much.

Delegates all P&L to strategy.simulate_month via entry_overrides, exactly
like fill_realism_v5.py -- nothing here reimplements stops/targets/exits.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import config
import daily_report
import harness
import strategy

# 26-Aug-2026: found via a fresh-vs-archived re-run that STILL disagreed
# (-4.53% archived vs +2.81% fresh) even after pinning use_classifier=
# False on the res_v6 simulate_month call below. Root cause: that fix
# only covers strategy.adjust_holding_window (the HOLDING-period P&L
# walk). The SEPARATE scoring-time path -- strategy.split_adjust, called
# from compute_signals for every universe symbol's lookback, and gated
# by config.CORP_ACTION_LLM_ENABLED, not adjust_holding_window's
# use_classifier -- still ran the live classifier and could change which
# 10 symbols even get PICKED by harness.v4_basket. Backtests must be
# deterministic end to end, picks included, so this script forces the
# flag off in its own process only; config.py's persisted value (True)
# is untouched and still governs the live report.
config.CORP_ACTION_LLM_ENABLED = False

Z80 = 0.8416212  # 80th percentile, standard normal -- entry_tracking.Z80
OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "fill_realism_v6_3stage.jsonl")


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
        if len(after) < 3:
            out["error"] = f"only {len(after)} trading day(s) after expiry -- Day 3 unreachable"
            return out

        day1, day2, day3 = after[0], after[1], after[2]
        hist = strategy.load_price_history(ex, harness.universe())
        hist_dates = sorted(hist.keys())
        sig_frame = hist.get(ex) if ex in hist else hist.get(hist_dates[-1])

        rows = []
        for sym in picks:
            if day1 not in merged or sym not in merged[day1].index:
                continue
            if sig_frame is None or sym not in sig_frame.index:
                continue
            close0 = float(sig_frame.loc[sym, "close_price"])
            if close0 <= 0:
                continue
            lo, hi, _ = daily_report._compute_stock_entry_band(sym, hist, close0)
            rows.append({"symbol": sym, "close": close0, "entry_lo": lo, "entry_hi": hi})

        if not rows:
            out["error"] = "no valid rows"
            return out

        # Day-0 sizing -- TODAY's daily_report (post 25-Aug fix), same
        # function open_window() calls for a real Day-0 quote.
        sizing = daily_report._compute_min_portfolio_sizing(rows)
        slot_target = sizing["slot_target"]
        shares0 = sizing["shares"]

        entry_overrides = {}
        detail = {}

        for sym, s in shares0.items():
            row1 = merged[day1].loc[sym]
            opn1 = float(row1.get("open_price", 0) or 0)
            low1 = float(row1.get("low_price", 0) or 0)
            close1 = float(row1.get("close_price", 0) or 0)
            fh = {}
            if opn1 <= 0:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "no_day1_open", "fill_history": fh}
                continue

            # abort_anchor (Day-1's open) is ONLY the gap-risk-abort check's
            # threshold, unchanged across stages -- "has this already broken
            # the level we accepted when we first decided to enter this
            # month" is a Day-1 question by definition. It is NOT what gets
            # recorded as risk_anchor on an actual fill (26-Aug-2026 fix,
            # ported from entry_tracking.py the same night): a fill anchors
            # to the min-basket QUOTED price for whichever stage it fills
            # at, since that is the number its own share count was sized
            # against -- one number governs both, and the execution price
            # only matters for P&L, never for the risk band.
            abort_anchor = opn1
            anchor_stop = abort_anchor * (1 - stop_pct / 100.0)
            quote1 = s["limit_price"]

            if low1 > 0 and low1 <= quote1:
                fill_price = min(opn1, quote1)
                recorded_anchor = quote1
                fh["day1"] = {"date": str(day1), "proposed_price": round(quote1, 2),
                             "filled": True, "fill_price": round(fill_price, 2)}
                entry_overrides[sym] = (fill_price, day1, recorded_anchor)
                detail[sym] = {"filled_day": 1, "price": fill_price,
                               "shares": s["shares"], "risk_anchor": recorded_anchor,
                               "fill_history": fh}
                continue
            fh["day1"] = {"date": str(day1), "proposed_price": round(quote1, 2), "filled": False}
            if low1 > 0 and low1 <= anchor_stop:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "before_day2",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue

            # Day 2: re-quote off Day-1's own Parkinson vol (entry_tracking
            # n==1's "missed" branch).
            sigma1 = parkinson_sigma(row1)
            if sigma1 is None or close1 <= 0:
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day1: merged[day1]}, close1 or opn1)
                quote2 = hi
            else:
                quote2 = close1 * math.exp(Z80 * sigma1)
            shares2 = max(1, round(slot_target / quote2))

            if day2 not in merged or sym not in merged[day2].index:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "no_day2_data",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue
            row2 = merged[day2].loc[sym]
            opn2 = float(row2.get("open_price", 0) or 0)
            low2 = float(row2.get("low_price", 0) or 0)
            close2 = float(row2.get("close_price", 0) or 0)
            if opn2 <= 0:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "no_day2_open",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue

            if low2 > 0 and low2 <= quote2:
                fill_price = min(opn2, quote2)
                recorded_anchor = quote2
                fh["day2"] = {"date": str(day2), "proposed_price": round(quote2, 2),
                             "filled": True, "fill_price": round(fill_price, 2)}
                entry_overrides[sym] = (fill_price, day2, recorded_anchor)
                detail[sym] = {"filled_day": 2, "price": fill_price,
                               "shares": shares2, "risk_anchor": recorded_anchor,
                               "quote2": round(quote2, 2), "fill_history": fh}
                continue
            fh["day2"] = {"date": str(day2), "proposed_price": round(quote2, 2), "filled": False}
            if low2 > 0 and low2 <= anchor_stop:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "before_day3",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue

            # Day 3: MANDATORY, pools Day-1 + Day-2 vol, anchored off
            # Day-2's own close (entry_tracking n==2's "missed" branch),
            # decided the evening of Day 2 -- no lookahead at Day-3's price.
            sigma2 = parkinson_sigma(row2)
            sigmas = [x for x in (sigma1, sigma2) if x is not None]
            pooled = sum(sigmas) / len(sigmas) if sigmas else None
            if pooled is None or close2 <= 0:
                lo, hi, _ = daily_report._compute_stock_entry_band(sym, {day2: merged[day2]}, close2 or opn2)
                quote3 = hi
            else:
                quote3 = close2 * math.exp(Z80 * pooled)
            shares3 = max(1, round(slot_target / quote3))

            if day3 not in merged or sym not in merged[day3].index:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "no_day3_data",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue
            row3 = merged[day3].loc[sym]
            opn3 = float(row3.get("open_price", 0) or 0)
            if opn3 > 0 and opn3 <= anchor_stop:
                fh["day3"] = {"date": str(day3), "proposed_price": round(quote3, 2), "filled": False}
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "day3_open_itself",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue
            if opn3 <= 0:
                entry_overrides[sym] = None
                detail[sym] = {"filled_day": None, "aborted_stage": "no_day3_open",
                               "risk_anchor": abort_anchor, "fill_history": fh}
                continue

            recorded_anchor = quote3
            fh["day3"] = {"date": str(day3), "proposed_price": round(quote3, 2),
                         "filled": True, "fill_price": round(opn3, 2)}
            entry_overrides[sym] = (opn3, day3, recorded_anchor)
            detail[sym] = {"filled_day": 3, "price": opn3, "shares": shares3,
                           "risk_anchor": recorded_anchor, "quote3": round(quote3, 2),
                           "fill_history": fh}

        total_invested = sum(d["shares"] * d["price"] for d in detail.values()
                             if d.get("filled_day") is not None)
        for sym, d in detail.items():
            if d.get("filled_day") is not None:
                invested = d["shares"] * d["price"]
                weight = (invested / total_invested * 100.0) if total_invested else 0.0
                d["invested"] = round(invested, 2)
                d["weight_pct"] = round(weight, 3)

        # use_classifier=False + config.CORP_ACTION_LLM_ENABLED=False at
        # module scope (26-Aug-2026): makes this backtest deterministic
        # and offline end to end -- picks (split_adjust, scoring-time)
        # and holding-window stop/target math (adjust_holding_window)
        # both skip the network/LLM classifier and fall back to the
        # pure, reproducible hard-band heuristic. Real, worth keeping
        # (strategy.py's own docstring: "Backtests must be deterministic
        # and offline"), but NOT what actually caused BSE's fake -60.48%
        # "loss" in the 2025-04-24 archive -- see the note below.
        res_baseline = harness.run_cycle(picks, ex, nx, stop_pct=stop_pct,
                                         ranked_order=list(picks), top_n=10,
                                         price_by_date=merged)
        after_nx = [d for d in sorted(merged) if d > nx]
        roll = after_nx[0] if after_nx else nx
        hold = [d for d in sorted(merged) if ex < d <= roll]
        res_v6 = strategy.simulate_month(
            list(picks), merged, hold, harness.sectors(),
            basket_symbols=list(picks), top_n=10,
            stop_pct=stop_pct, target_pct=None,
            carry_forward=False, entry_overrides=entry_overrides,
            use_classifier=False)

        # 26-Aug-2026, the REAL bug, found by re-running 2025-04-24 fresh
        # and getting a different number every time no matter what the
        # classifier did: simulate_month() calls adjust_holding_window()
        # INTERNALLY and walks its OWN corp-action-adjusted copy of the
        # price series for every stop/target/pnl decision (that's
        # `v6_return_pct` below) -- but it does not mutate or hand back
        # the caller's `merged` dict, which stays RAW. Reading `merged
        # [roll]` for a ROLLOVER exit price therefore uses the wrong
        # series: BSE's real 23-May-2025 2:1 split showed as an
        # unadjusted 2472.50 open here while simulate_month's own
        # internal walk correctly saw ~7066.52 (factor 2.858). Fix:
        # rebuild the SAME adjusted series ourselves, with the exact
        # same call simulate_month makes, and read the exit off that.
        _held = set(picks)
        adjusted_merged, _ = strategy.adjust_holding_window(
            merged, hold, symbols=sorted(_held), classify_symbols=_held,
            use_classifier=False, return_factors=True)

        # per-symbol archive records, one per filled name, in the exact
        # shape book.write_simulated_record expects -- the "full
        # derivation trail" the user required ("I DON'T WANT ANY
        # RECREATION OF ANY DATA AGAIN IN A PARTICULAR VERSION"). A STOP/
        # TARGET/mid-month ROLLOVER-carry mismatch shows up in
        # res_v6.exits with its own real (already-adjusted) exit price;
        # anything not there simply ran to the month's own close-out
        # (carry_forward=False, so simulate_month force-sells every slot
        # at the next cycle's open, `roll`) and its exit price is read
        # from the SAME adjusted series simulate_month itself walked,
        # not raw bhavcopy.
        exits_by_symbol = {e.symbol: e for e in res_v6.exits}
        entry_day_by_stage = {1: day1, 2: day2, 3: day3}
        archive_records = {}
        for sym, d in detail.items():
            if d.get("filled_day") is None:
                continue
            entry_date = entry_day_by_stage[d["filled_day"]]
            if sym in exits_by_symbol:
                e = exits_by_symbol[sym]
                exit_price, exit_date, reason = round(e.exit_px, 2), e.exit_date, e.reason
            else:
                exit_price, exit_date, reason = None, roll, "ROLLOVER"
                if roll in adjusted_merged and sym in adjusted_merged[roll].index:
                    px = adjusted_merged[roll].loc[sym, "open_price"]
                    try:
                        if px is not None and float(px) == float(px) and float(px) > 0:
                            exit_price = round(float(px), 2)
                    except (TypeError, ValueError):
                        pass
            archive_records[sym] = {
                "symbol": sym,
                "origin_expiry": str(ex),
                "backtest_version": config.BACKTEST_VERSION,
                "shares": d["shares"],
                "entry_price": round(d["price"], 2),
                "entry_date": str(entry_date),
                "risk_anchor": round(d["risk_anchor"], 2),
                "fill_history": d["fill_history"],
                "exit_price": exit_price,
                "exit_date": str(exit_date) if exit_date is not None else None,
                "reason": reason,
            }

        out.update({
            "expiry": str(ex), "day1": str(day1), "day2": str(day2), "day3": str(day3),
            "stop_pct": stop_pct, "breadth": round(breadth, 2),
            "slot_target": round(slot_target, 2),
            "n_filled_day1": sum(1 for d in detail.values() if d.get("filled_day") == 1),
            "n_filled_day2": sum(1 for d in detail.values() if d.get("filled_day") == 2),
            "n_filled_day3": sum(1 for d in detail.values() if d.get("filled_day") == 3),
            "n_aborted": sum(1 for d in detail.values() if d.get("filled_day") is None),
            "stocks": detail,
            "archive_records": archive_records,
            "baseline_return_pct": round(res_baseline.return_pct, 3),
            "v6_return_pct": round(res_v6.return_pct, 3),
            "delta_pts": round(res_v6.return_pct - res_baseline.return_pct, 3),
        })
    except Exception as exc:
        import traceback
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-1500:]
    return out


def archive_month(y, m):
    """
    run_month(y, m), then persist every filled symbol's full derivation
    to book_archive.jsonl as kind="simulated" under config.
    BACKTEST_VERSION -- the once-ever step satisfying "no more simulation
    is needed ever" for the current version. Idempotent: skips any
    (symbol, origin_expiry) pair already archived under this same
    version, so re-running this script never duplicates a line.

    26-Aug-2026: simulation only runs to fill a GAP. If real data
    already exists for this expiry (book.holdings_for_expiry returns
    kind="actual" rows), a simulated shadow is redundant and actively
    confusing -- it invites exactly the "why are there two different
    numbers for the same real month" question a simulated Jul-2026 copy
    caused the moment it was compared against that cycle's real archive.
    Skips entirely (no run_month call at all) when any actual coverage
    exists for the expiry.
    """
    import book
    ex, _ = harness.cycle_dates(y, m)
    if book.holdings_for_expiry(ex):
        return {"month": f"{y}-{m:02d}", "skipped": "actual data exists for this expiry"}
    result = run_month(y, m)
    if "error" in result:
        return result
    existing = {(r["symbol"], r["origin_expiry"])
               for r in book.simulated_records(backtest_version=config.BACKTEST_VERSION)}
    written = []
    for sym, rec in result.get("archive_records", {}).items():
        if (sym, rec["origin_expiry"]) in existing:
            continue
        book.write_simulated_record(rec)
        written.append(sym)
    result["archived"] = written
    return result


# The 17 cycles studied all session: the canonical 13-month comparison
# table (2025-03 through 2026-03) plus the 4 live months since
# (2026-04 through 2026-07). 2026-08 is deliberately excluded -- its
# cycle opened 25-Aug-2026 and has not concluded, and a month cannot be
# archived until it has (26-Aug-2026 rule 4).
ALL_HISTORICAL_CYCLES = [
    (2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
    (2025, 9), (2025, 10), (2025, 11), (2025, 12),
    (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6), (2026, 7),
]


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--archive-all":
        import book
        # Resumable: skip a month entirely (no run_month at all, so no
        # wasted classifier/LLM calls) once its expiry already has
        # archived rows under this version -- this loop is re-invoked
        # across several truncated shell calls in practice.
        done_expiries = {r["origin_expiry"]
                         for r in book.simulated_records(backtest_version=config.BACKTEST_VERSION)}
        for y, m in ALL_HISTORICAL_CYCLES:
            ex, _ = harness.cycle_dates(y, m)
            if str(ex) in done_expiries:
                print(f"{y}-{m:02d}: already archived ({ex}), skipping")
                continue
            r = archive_month(y, m)
            if "skipped" in r:
                print(f"{y}-{m:02d}: skipped -- {r['skipped']}")
            elif "error" in r:
                print(f"{y}-{m:02d}: ERROR {r['error']}")
            else:
                print(f"{y}-{m:02d}: archived {len(r.get('archived', []))} "
                     f"symbols ({r.get('n_filled_day1', 0)}/{r.get('n_filled_day2', 0)}/"
                     f"{r.get('n_filled_day3', 0)} day1/2/3, {r.get('n_aborted', 0)} aborted)")
    else:
        y, m = int(sys.argv[1]), int(sys.argv[2])
        result = run_month(y, m)
        os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
        with open(OUT_FILE, "a") as fh:
            fh.write(json.dumps(result) + "\n")
        printable = {k: v for k, v in result.items() if k not in ("trace", "stocks", "archive_records")}
        print(json.dumps(printable))
        if "trace" in result:
            print(result["trace"])
