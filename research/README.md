# research/ — NOT PRODUCTION. Do not quote numbers from these files.

Every script here **reimplements** the position walk instead of calling
`strategy.simulate_month`. That is the same defect that produced the
`sim()` helper's bogus "live V4 = Rs115.90" on 02-Aug-2026: both arms of a
comparison share the flaw, so the *ranking* usually survives, but the
*level* is not comparable to anything the production engine produces.

Concretely, on 03-Aug-2026 these files were used to produce a headline of
**+39.57% over 13 cycles**. That number is not a production result and
must not be quoted. The production path is `tools_run13.py` in the repo
root, which calls `strategy.simulate_month` directly.

## Two bugs were found and fixed in `strategy.py` AFTER most of these ran

1. **Unadjusted corporate actions over the holding window.** NSE bhavcopy
   is not adjusted; BSE's 2:1 on 23-05-2025 read as a -61.99% crash, trip-
   ped the stop, and cost the Apr-2025 cycle 6.2pp. Now handled by
   `strategy.adjust_holding_window`.
2. **Gap-through fills.** A resting stop fills AT its price when the level
   trades, but a session that GAPS through it fills at the open. TRENT
   closed 3343.80 on 06-07-2026 and opened 3080.00 against a 3120.75 stop
   — a -6.24% fill, not -5.00%.

`research/tools_matrix.py` and `research/tools_stopsweep.py` carry their
own private copies of these fixes. The rest do not, so their numbers are
wrong in both directions.

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
