"""
Run the LIVE engine over a range of cycles, one cycle per invocation.

Uses strategy.simulate_month -- the same function daily_report calls --
so carry-forward, the sector cap, the re-entry policy and the redeploy
switch all behave exactly as they do in production. Earlier ad-hoc
scripts reimplemented the loop and silently dropped carry-forward, which
made their levels incomparable to anything.

    python tools_run13.py            # advance one cycle, append to state
    python tools_run13.py report     # print the table

State lives in /tmp/run13.json so a 45s shell timeout cannot lose work.
"""
import json
import logging
import os
import sys

logging.disable(logging.CRITICAL)
import strategy                                            # noqa: E402
import scoring                                             # noqa: E402
import nse_client                                          # noqa: E402
import config                                              # noqa: E402

STATE = "/tmp/run13.json"
MONTHS = [(2025, 3), (2025, 4), (2025, 5), (2025, 6), (2025, 7), (2025, 8),
          (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1),
          (2026, 2), (2026, 3)]


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"i": 0, "rows": [], "carry": {}, "breadth": {}}


def main():
    s = load()
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        rows = s["rows"]
        print(f"{'cycle':<9}{'breadth':>9}{'stop':>6}{'return':>9}{'trades':>8}{'carried':>9}")
        acc = 100.0
        for r in rows:
            acc *= (1 + r["ret"] / 100)
            print(f"{r['k']:<9}{r['breadth']:>9.1f}{r['stop']:>6.0f}"
                  f"{r['ret']:>8.2f}%{r['trades']:>8}{r['carried']:>9}")
        if rows:
            x = [r["ret"] for r in rows]
            import statistics as st
            t = st.mean(x) / (st.stdev(x) / len(x) ** 0.5) if len(x) > 1 else float("nan")
            print(f"\nACCRUED (sum)     {sum(x):+.2f}%")
            print(f"COMPOUNDED Rs100  {acc:.2f}")
            print(f"mean {st.mean(x):.2f}%/mo | sd {st.stdev(x):.2f} | "
                  f"worst {min(x):.2f}% | positive {sum(1 for v in x if v > 0)}/{len(x)} "
                  f"| t {t:.2f}")
        return

    i = s["i"]
    if i >= len(MONTHS):
        print("complete -- run `report`")
        return
    y, m = MONTHS[i]
    td = strategy.known_trading_days()
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    ex = strategy.expiry_for(y, m, trading_days=td)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nx = strategy.expiry_for(ny, nm, trading_days=td)

    hist = strategy.load_price_history(ex, uni)
    stop = strategy.resolve_stop_pct(ex, uni, hist)
    breadth = strategy.market_breadth(ex, uni, hist)

    fo = scoring.normalize_fo_columns(nse_client.fetch_fo_bhavcopy(ex))
    sig = strategy.compute_signals_cached(hist, fo, ex, uni)
    basket, full = strategy.rank_universe(sig, sec)

    fwd = strategy.load_price_history(nx, uni, days=60)
    merged = dict(hist)
    merged.update(fwd)
    days = [d for d in sorted(merged) if ex < d <= nx]

    carry = {k: strategy.Position(**v) for k, v in s["carry"].items()}
    res = strategy.simulate_month(
        list(full.index), merged, days, sec,
        carry_in=carry, basket_symbols=basket["symbol"].tolist(),
        carry_forward=config.V4_CARRY_FORWARD, stop_pct=stop)

    s["rows"].append({"k": f"{y}-{m:02d}", "breadth": breadth, "stop": stop,
                      "ret": res.return_pct, "trades": res.trades,
                      "carried": len(res.carry)})
    s["carry"] = {k: {"symbol": p.symbol, "entry": p.entry, "stop": p.stop,
                      "target": p.target, "entry_date": str(p.entry_date)}
                  for k, p in res.carry.items()}
    s["i"] = i + 1
    json.dump(s, open(STATE, "w"))
    print(f"{y}-{m:02d}  breadth {breadth:.1f}%  stop {stop:.0f}%  "
          f"return {res.return_pct:+.2f}%  trades {res.trades}  "
          f"carrying {len(res.carry)}   [{i + 1}/{len(MONTHS)}]")


if __name__ == "__main__":
    main()
