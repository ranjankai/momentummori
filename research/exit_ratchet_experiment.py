"""
Clean, isolated test of ONE idea: keep the live 5%(regime)/40% stop and
target exactly as they are, and layer nothing but a ratchet on top --
once a session's HIGH reaches entry*(1+trigger%), raise the stop (never
lower it) to entry*(1+lock%) for every later session.

This is deliberately NOT the 03-Aug-2026 "ATR conviction SOP" (tiered
ATR-multiple stops + promotion gate + trailing) that cost -15.36pp --
that combined design confounded a tighter INITIAL stop with the ratchet
idea. Here the initial stop/target are untouched; ratchet_trigger_pct/
ratchet_lock_pct are the only new knobs, added to strategy.simulate_month
directly (both None = identical to the live engine).

Same 13 cycles as research/run13.py (Mar-2025 .. Mar-2026), same
fresh-start-additive convention, same live V4 basket per cycle. Classifier
is OFF for the sweep (determinism, speed -- ~15 ratchet configs x 13
cycles would otherwise fire the corp-action classifier ~200 times); a
classifier-OFF baseline is printed alongside every ratchet config so the
comparison is apples to apples. The canonical classifier-ON number
(+39.57%) is only for cross-reference, not compared directly.

    python research/exit_ratchet_experiment.py
"""
import json
import os
import pickle
import statistics as st
import sys

import harness                                              # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy                                              # noqa: E402

CACHE = "/tmp/exit_ratchet_cycles.pkl"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]

CONFIGS = [
    ("baseline (no ratchet)", None, None),
    ("trigger 15% -> lock breakeven", 15, 0),
    ("trigger 15% -> lock +5%", 15, 5),
    ("trigger 20% -> lock breakeven", 20, 0),
    ("trigger 20% -> lock +8%", 20, 8),
    ("trigger 20% -> lock +12%", 20, 12),
    ("trigger 25% -> lock +10%", 25, 10),
    ("trigger 25% -> lock +15%", 25, 15),
    ("trigger 30% -> lock +15%", 30, 15),
    ("trigger 30% -> lock +20%", 30, 20),
]


def build_cycles():
    """Fetch (picks, ranked, stop_pct, merged prices, hold dates) once per
    cycle and cache to disk -- this is the slow part (NSE fetch + signals),
    and is identical across every ratchet config, so do it exactly once.
    Checkpoints after EVERY cycle (not just at the end) so a slow/killed
    run can resume instead of re-fetching from scratch."""
    cycles = []
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cycles = pickle.load(f)
        except EOFError:
            cycles = []   # a prior run was killed mid-write; refetch from scratch
    done = {c["k"] for c in cycles}
    for y, m in MONTHS:
        k = f"{y}-{m:02d}"
        if k in done:
            continue
        ex, nx = harness.cycle_dates(y, m)
        picks, ranked, stop_pct, breadth = harness.v4_basket(ex)
        merged = harness.prices(ex, nx)
        after = [d for d in sorted(merged) if d > nx]
        roll = after[0] if after else nx
        hold = [d for d in sorted(merged) if ex < d <= roll]
        # simulate_month only ever looks up price_by_date.get(day) for
        # day in hold -- caching the full ~260-session lookback (needed
        # only transiently, to compute the picks above) bloated the
        # checkpoint file past 1GB after 4 cycles and made every
        # load/save too slow to finish inside one call. Trim to the
        # hold window before caching.
        trimmed = {d: merged[d] for d in hold if d in merged}
        cycles.append(dict(k=k, picks=picks, ranked=ranked,
                            stop_pct=stop_pct, breadth=breadth,
                            merged=trimmed, hold=hold))
        tmp = CACHE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(cycles, f)
        os.replace(tmp, CACHE)      # atomic -- a kill mid-write can't corrupt CACHE
        print(f"  fetched+checkpointed {k}  breadth {breadth:.1f}%  stop {stop_pct:.0f}%  "
              f"[{len(cycles)}/{len(MONTHS)}]", file=sys.stderr, flush=True)
    cycles.sort(key=lambda c: MONTHS.index(tuple(int(x) for x in c["k"].split("-"))))
    return cycles


def run_config(cycles, trigger, lock):
    rows = []
    for c in cycles:
        res = strategy.simulate_month(
            c["ranked"], c["merged"], c["hold"], harness.sectors(),
            basket_symbols=list(c["picks"]), top_n=10,
            stop_pct=c["stop_pct"], target_pct=None,
            ratchet_trigger_pct=trigger, ratchet_lock_pct=lock,
            carry_forward=False, use_classifier=False)
        rows.append(dict(k=c["k"], ret=res.return_pct, trades=res.trades,
                          exits=res.exits, stop_pct=c["stop_pct"]))
    return rows


def classify_exits(rows):
    """STOP exits split into 'clean stop' (at/near the original fixed
    level) vs 'ratchet stop' (exit price better than the original fixed
    stop would have given -- the ratchet actually did something), plus
    TARGET and ROLLOVER/EXPIRY counts."""
    target = stop_clean = stop_ratchet = other = 0
    for r in rows:
        orig_stop_mult = 1 - r["stop_pct"] / 100
        for e in r["exits"]:
            if e.reason == "TARGET":
                target += 1
            elif e.reason == "STOP":
                orig_stop_px = e.entry * orig_stop_mult
                if e.exit_px > orig_stop_px * 1.001:   # meaningfully better than the fixed stop
                    stop_ratchet += 1
                else:
                    stop_clean += 1
            else:
                other += 1
    return target, stop_clean, stop_ratchet, other


def main():
    print("Fetching/caching cycle data (first run only, slow)...", file=sys.stderr)
    cycles = build_cycles()
    print(f"\n{len(cycles)} cycles cached. Running {len(CONFIGS)} configs "
          f"(classifier OFF for the sweep)...\n")

    print(f"{'config':<34}{'additive':>10}{'worst':>9}{'best':>8}"
          f"{'pos/13':>8}{'target':>8}{'stop-clean':>11}{'stop-ratchet':>13}")
    results = []
    for label, trig, lock in CONFIGS:
        rows = run_config(cycles, trig, lock)
        x = [r["ret"] for r in rows]
        target, stop_clean, stop_ratchet, other = classify_exits(rows)
        results.append((label, trig, lock, x, rows, (target, stop_clean, stop_ratchet, other)))
        print(f"{label:<34}{sum(x):>9.2f}%{min(x):>8.2f}%{max(x):>7.2f}%"
              f"{sum(1 for v in x if v>0):>6}/13"
              f"{target:>8}{stop_clean:>11}{stop_ratchet:>13}")

    print("\nPer-cycle detail vs baseline:\n")
    baseline = results[0][3]
    for label, trig, lock, x, rows, _ in results:
        diff = [round(a - b, 2) for a, b in zip(x, baseline)]
        print(f"{label}")
        for r, d in zip(rows, diff):
            flag = "" if abs(d) < 0.01 else f"  (Δ{d:+.2f})"
            print(f"    {r['k']}  {r['ret']:+7.2f}%{flag}")
        print()


if __name__ == "__main__":
    main()
