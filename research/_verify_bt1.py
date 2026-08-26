"""
One-off verification script (26-Aug-2026): re-run every BT1 month FRESH
and cross-check three things against what's sitting in book_archive.jsonl,
rather than trusting the archive write path blindly:

1. `v6_return_pct` (computed live by strategy.simulate_month, the actual
   source of truth) matches the archive-derived mean-of-10 EXACTLY.
2. Every month has n_aborted == 0 -- if any slot ever aborted, an
   archive that only stores FILLED symbols would silently average over
   fewer than 10 slots, diverging from simulate_month's own convention
   (an aborted slot still occupies one of the 10 and contributes 0, it
   is not excluded from the denominator).
3. The archived risk_anchor for every record matches the STAGE-QUOTE
   rule exactly (quote1 for a day-1 fill, quote2 for day-2, quote3 for
   day-3) -- not opn1, not the fill price.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.disable(logging.CRITICAL)

import fill_realism_v6_3stage as fr

ALL_CYCLES = [c for c in fr.ALL_HISTORICAL_CYCLES if c != (2026, 7)]
RESULTS_FILE = "/tmp/verify_bt1_results.jsonl"

start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
end = int(sys.argv[2]) if len(sys.argv) > 2 else len(ALL_CYCLES)
CYCLES = ALL_CYCLES[start:end]

recs = [json.loads(l) for l in open("data/book_archive.jsonl") if l.strip()]
sim = [r for r in recs if r.get("kind") == "simulated"]
by_expiry = {}
for r in sim:
    by_expiry.setdefault(r["origin_expiry"], []).append(r)

mismatches = []
for y, m in CYCLES:
    res = fr.run_month(y, m)
    if "error" in res:
        mismatches.append(f"{y}-{m:02d}: RUN ERROR {res['error']}")
        continue
    ex = res["expiry"]
    archived = by_expiry.get(ex, [])
    n = res["n_filled_day1"] + res["n_filled_day2"] + res["n_filled_day3"]
    if res["n_aborted"] != 0:
        mismatches.append(f"{ex}: n_aborted={res['n_aborted']} (archive has only {len(archived)} rows, "
                          f"simulate_month divides by 10 including zero-contribution aborts)")
    if len(archived) != n:
        mismatches.append(f"{ex}: archive has {len(archived)} rows, run_month filled {n} -- COUNT MISMATCH")
    arch_mean = sum((r["exit_price"] - r["entry_price"]) / r["entry_price"] * 100.0
                    for r in archived) / len(archived) if archived else None
    # simulate_month's own convention: mean over 10 slots, 0 for any aborted/unfilled
    true_mean_over_10 = res["v6_return_pct"]
    naive_mean_over_filled = arch_mean
    if archived and abs(true_mean_over_10 - naive_mean_over_filled) > 0.02 and res["n_aborted"] == 0:
        mismatches.append(f"{ex}: v6_return_pct={true_mean_over_10} vs archive-derived mean={round(naive_mean_over_filled,3)} "
                          f"-- MISMATCH even with 0 aborts")
    # anchor check
    quote_map = {1: "day1", 2: "day2", 3: "day3"}
    detail = res["stocks"]
    for sym, d in detail.items():
        if d.get("filled_day") is None:
            continue
        stage_key = quote_map[d["filled_day"]]
        expected_quote = d["fill_history"][stage_key]["proposed_price"]
        arch_row = next((r for r in archived if r["symbol"] == sym), None)
        if arch_row is None:
            mismatches.append(f"{ex}/{sym}: filled in fresh run but MISSING from archive")
            continue
        if abs(arch_row["risk_anchor"] - expected_quote) > 0.01:
            mismatches.append(f"{ex}/{sym}: archived risk_anchor={arch_row['risk_anchor']} "
                              f"!= stage-{d['filled_day']} quote={expected_quote}")
    line = (f"{ex}: n_aborted={res['n_aborted']}, v6_return_pct={true_mean_over_10}, "
           f"archive_mean={round(naive_mean_over_filled,3) if naive_mean_over_filled is not None else None}, "
           f"rows={len(archived)}/{n}")
    print(line)
    with open(RESULTS_FILE, "a") as fh:
        fh.write(json.dumps({"expiry": ex, "line": line,
                            "mismatches": [m for m in mismatches if ex in m]}) + "\n")

print()
if mismatches:
    print(f"{len(mismatches)} MISMATCH(ES) FOUND (this batch):")
    for m in mismatches:
        print(" -", m)
else:
    print("This batch: ALL CLEAR.")
