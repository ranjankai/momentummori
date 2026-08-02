"""
The former `run_strategy.py backtest` command. Removed from the live CLI
on 02-Aug-2026 and kept here only for reference.

It is deliberately NOT importable from the running system. It was the one
caller that drove the strategy down a second code path -- mechanical
redeployment rather than the live LLM pick -- and that second path is
exactly where a divergence between "what was tested" and "what runs"
would hide.

The verified numbers it produced (+31.77% over 13 months at a 5% stop,
no LLM layer) are recorded in CONTEXT.md. They describe a configuration
that no longer exists.
"""

def cmd_backtest(args):
    symbols = strategy.load_fo_universe()
    sectors = strategy.load_sector_map()
    trading_days = strategy.known_trading_days()
    start = datetime.strptime(args.start, "%Y-%m").date()
    end = datetime.strptime(args.end, "%Y-%m").date()

    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    results, total = [], 0.0
    carry = {}
    for (yy, mm) in months:
        try:
            py, pm = (yy - 1, 12) if mm == 1 else (yy, mm - 1)
            prev_exp = strategy.expiry_for(py, pm, trading_days=trading_days)
            this_exp = strategy.expiry_for(yy, mm, trading_days=trading_days)
            basket, full, hist = _snapshot(prev_exp, symbols, sectors)

            fwd = strategy.load_price_history(this_exp, symbols, days=60)
            merged = dict(hist); merged.update(fwd)
            days = [d for d in sorted(merged) if prev_exp < d <= this_exp]
            if len(days) < 5:
                raise strategy.StrategyError("not enough trading days in window")

            res = strategy.simulate_month(
                list(full.index), merged, days, sectors,
                carry_in=carry, basket_symbols=basket["symbol"].tolist(),
                carry_forward=not args.no_carry_forward)
            total += res.return_pct
            results.append(res)
            carry = res.carry
            print(f"{yy}-{mm:02d}  {res.return_pct:>7.2f}%   {res.trades:>3} trades"
                  f"   carrying {len(carry)}")
        except (strategy.StrategyError, nse_client.NseFetchError) as exc:
            print(f"{yy}-{mm:02d}  SKIPPED ({exc})")

    if carry:
        print(f"\n{len(carry)} position(s) still open at the end of the range "
              f"(marked to last close, not sold): {', '.join(sorted(carry))}")

    if results:
        rets = pd.Series([r.return_pct for r in results])
        print(f"\nACCRUED (sum of {len(results)} months): {total:+.2f}%")
        print(f"mean {rets.mean():.2f}%/mo | sd {rets.std():.2f} | "
              f"worst {rets.min():.2f}% | positive {int((rets > 0).sum())}/{len(rets)}")
        out = os.path.join(config.DATA_DIR, "v4_backtest.json")
        with open(out, "w") as fh:
            json.dump([{"month": r.month, "return_pct": r.return_pct,
                        "trades": r.trades, "slots": r.slots} for r in results],
                      fh, indent=2)
        print(f"Saved -> {out}")


