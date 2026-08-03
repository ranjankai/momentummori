# research/ — use `harness.py`. Never fork the walk loop.

## What happened, and what was deleted

Seven scripts here **reimplemented** the position walk instead of calling
`strategy.simulate_month`: `tools_matrix`, `tools_sop13`, `tools_sop_run`,
`tools_cycle_compare`, `tools_stopsweep`, `tools_derisk` and
`tools_selection_test`. Same defect that produced the `sim()` helper's
bogus "live V4 = Rs115.90" on 02-Aug-2026 — both arms of a comparison
share the flaw, so the *ranking* usually survives but the *level* is
meaningless.

They produced a headline of **+39.57% over 13 cycles**, which was never a
production result. **All seven were deleted on 03-Aug-2026** and replaced
by `harness.py`, which delegates every walk to `strategy.simulate_month`.

The production 13-cycle number is **+36.87%** (`tools_run13.py`, repo
root, carry-forward chained). `harness.py` runs the fresh-start additive
convention instead, which is the agreed reporting basis.

## Rule

New backtests call `harness.run_cycle` or `harness.walk_forward`. If
`simulate_month` lacks a parameter you need, **add the parameter** — do
not copy the loop.

## Two bugs were found and fixed in `strategy.py` AFTER most of these ran

1. **Unadjusted corporate actions over the holding window.** NSE bhavcopy
   is not adjusted; BSE's 2:1 on 23-05-2025 read as a -61.99% crash, trip-
   ped the stop, and cost the Apr-2025 cycle 6.2pp. Now handled by
   `strategy.adjust_holding_window`.
2. **Gap-through fills.** A resting stop fills AT its price when the level
   trades, but a session that GAPS through it fills at the open. TRENT
   closed 3343.80 on 06-07-2026 and opened 3080.00 against a 3120.75 stop
   — a -6.24% fill, not -5.00%.

Both fixes now live in `strategy.py` and are covered by
`tests/test_execution.py`, so anything routed through `harness.py` gets
them automatically.

## Therefore, treat as UNVERIFIED

- the 2x2 entry/exit matrix (V4 vs the ATR SOP vs the v1.1 selector)
- the ATR conviction SOP rejection (-15.36pp)
- the v1.1 selection rejection (-24.93pp)
- the Altcase return gap (+20.26pp over 12 months)
- the stop-width sweep and the weekly breadth de-risk sweep
- the overlap-matching results (11/40)

The *conclusions* are probably still right — nothing came close to beating
the live config, and the failure mechanisms (a promotion gate 18% away, a
calm-volatility tilt that cuts the right tail) are structural, not
artefacts of the fill model. But before any of it informs a decision, it
must be re-run through `strategy.simulate_month`.

## Rule going forward

New backtests call `strategy.simulate_month`. If it needs a parameter it
does not have, add the parameter — do not fork the loop.
