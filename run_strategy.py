"""
CLI for the V4 strategy.

Generate the basket for the next month (run this the evening of expiry --
the bhavcopy is published after market close, and the orders go in at the
next 09:15 open):

    python run_strategy.py basket --expiry 2026-07-28

Backtest a range of months:

    python run_strategy.py backtest --start 2025-04 --end 2026-04

Both read cached bhavcopy from data/cache/ and fetch anything missing.
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime

import pandas as pd

import config
import nse_client
import scoring
import strategy


def _setup_logging(verbose: bool):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_FILE),
                  logging.StreamHandler(sys.stderr)],
    )


def _snapshot(expiry: date, symbols, sector_map):
    """Score the universe as of an expiry date. Returns (basket, full_ranking)."""
    hist = strategy.load_price_history(expiry, symbols)
    if expiry not in hist:
        raise strategy.StrategyError(
            f"{expiry} has no cash-market bhavcopy -- is it a trading day?")
    fo_raw = nse_client.fetch_fo_bhavcopy(expiry)
    fo = scoring.normalize_fo_columns(fo_raw)
    sig = strategy.compute_signals(hist, fo, expiry, symbols)
    basket, full = strategy.rank_universe(sig, sector_map)
    return basket, full, hist


def _load_holdings():
    """{symbol: {"entry":.., "entry_date":..}} persisted from the last basket run."""
    if not os.path.exists(config.V4_HOLDINGS_FILE):
        return {}
    with open(config.V4_HOLDINGS_FILE) as fh:
        return json.load(fh)


def _save_holdings(holdings: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.V4_HOLDINGS_FILE, "w") as fh:
        json.dump(holdings, fh, indent=2)


def cmd_basket(args):
    expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date()
    symbols = strategy.load_fo_universe()
    sectors = strategy.load_sector_map()
    basket, _, _ = _snapshot(expiry, symbols, sectors)
    basket["sector"] = basket["symbol"].map(lambda s: sectors.get(s, "Unclassified"))

    cols = ["rank", "symbol", "sector", "close", "stop_loss", "target",
            "weight_pct", "score", "volatility", "rollover", "cost_of_carry"]
    print(f"\nBasket from the {expiry} close\n")
    print(basket[cols].to_string(index=False,
                                 float_format=lambda v: f"{v:,.2f}"))

    if config.V4_CARRY_FORWARD:
        holdings = _load_holdings()
        basket_syms = set(basket["symbol"])
        hold = [s for s in holdings if s in basket_syms]
        sell = [s for s in holdings if s not in basket_syms]
        buy = [s for s in basket_syms if s not in holdings]

        print("\n--- Action list (v5, cross-month carry-forward is ON) ---")
        if hold:
            print(f"HOLD ({len(hold)}): {', '.join(sorted(hold))}  -- no order, "
                  "already positioned, stop/target reset to this month's levels")
        if sell:
            print(f"SELL ({len(sell)}): {', '.join(sorted(sell))}  -- at the "
                  "next session's open, dropped out of the basket")
        if buy:
            print(f"BUY  ({len(buy)}): {', '.join(sorted(buy))}  -- at the "
                  "next session's open")
        if not holdings:
            print("(No prior holdings file found -- treating this as a fresh "
                  f"start; every name above is a BUY. File: {config.V4_HOLDINGS_FILE})")

        new_holdings = {}
        for _, row in basket.iterrows():
            sym = row["symbol"]
            if sym in holdings:
                new_holdings[sym] = holdings[sym]  # cost basis unchanged -- held
            else:
                new_holdings[sym] = {
                    "entry": None,  # unknown until the broker fills it tomorrow's open
                    "entry_date": None,
                    "note": "pending fill -- update 'entry' once the order executes",
                }
        _save_holdings(new_holdings)
        print(f"\nHoldings file updated -> {config.V4_HOLDINGS_FILE}")
        print("IMPORTANT: after your broker fills the BUY orders tomorrow, edit "
              "that file and set each pending position's 'entry' to the actual "
              "fill price -- this tool does not talk to your broker.")
    else:
        print(f"\nStop {config.V4_STOP_LOSS_PCT}% / target {config.V4_TARGET_PCT}% "
              f"as resting orders. Re-entry policy: {config.V4_REENTRY_POLICY}.")
        print("Cross-month carry-forward is OFF: sell everything at the open "
              "after the next expiry, rebuy fresh next month.")

    out = os.path.join(config.DATA_DIR, f"basket_{expiry:%Y%m%d}.json")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"expiry": str(expiry),
                   "weights": config.V4_WEIGHTS,
                   "stop_pct": config.V4_STOP_LOSS_PCT,
                   "target_pct": config.V4_TARGET_PCT,
                   "reentry": config.V4_REENTRY_POLICY,
                   "carry_forward": config.V4_CARRY_FORWARD,
                   "basket": json.loads(basket.to_json(orient="records"))}, fh, indent=2)
    print(f"\nSaved -> {out}")


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


def cmd_perf(args):
    """Portfolio performance note. Run on expiry evening, after `sheet`."""
    import alerts
    import daily_report
    import ledger

    text = daily_report.render_performance(ledger.performance())
    print(text)
    if args.no_send:
        print("\n(--no-send: not delivered)")
        return
    if not alerts.send(text):
        print("\nDELIVERY FAILED -- see logs/app.log", file=sys.stderr)
        sys.exit(2)
    print(f"\nDelivered to Telegram chat {config.TELEGRAM_CHAT_ID}")


def cmd_history(args):
    """Look back at what the system actually told you to do."""
    import ledger

    if args.trades:
        rows = ledger.closed_trades()
        if not rows:
            print("No closed trades recorded yet.")
            return
        print(f"{'exit date':<12}{'symbol':<14}{'reason':<10}"
              f"{'entry':>10}{'exit':>10}{'P&L %':>9}")
        for t in rows:
            print(f"{t['exit_date']:<12}{t['symbol']:<14}{t['reason']:<10}"
                  f"{t['entry']:>10,.2f}{t['exit']:>10,.2f}{t['pnl_pct']:>9.2f}")
        tot = sum(t["pnl_pct"] for t in rows)
        wins = sum(1 for t in rows if t["pnl_pct"] > 0)
        print(f"\n{len(rows)} trades | {wins} winners "
              f"({wins / len(rows) * 100:.0f}%) | sum {tot:+.2f}%")
        return

    rows = ledger.monthly_summary()
    if not rows:
        print("Ledger is empty -- run `daily` at least once.")
        return
    print(f"{'expiry':<12}{'last run':<12}{'return %':>10}"
          f"{'closed':>8}{'wins':>6}{'win %':>8}{'open':>6}")
    for r in rows:
        wr = f"{r['win_rate']:.0f}" if r["win_rate"] is not None else "-"
        print(f"{r['expiry']:<12}{r['last_run']:<12}{r['return_pct']:>10.2f}"
              f"{r['closed']:>8}{r['wins']:>6}{wr:>8}{r['open']:>6}")
    print(f"\nAccrued across {len(rows)} month(s): "
          f"{sum(r['return_pct'] for r in rows):+.2f}%")


def cmd_sheet(args):
    """Monthly order sheet. Run on the evening of expiry."""
    import alerts
    import daily_report

    expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date()
    try:
        sheet = daily_report.build_entry_sheet(expiry)
        text = daily_report.render_entry_sheet(sheet)
    except Exception as exc:
        logging.getLogger("momentum_tracker").exception("Entry sheet failed")
        alerts.send_failure(f"entry sheet for {expiry}", exc)
        raise

    print(text)
    if args.no_send:
        print("\n(--no-send: not delivered)")
        return
    if not alerts.send(text):
        print("\nDELIVERY FAILED -- see logs/app.log", file=sys.stderr)
        sys.exit(2)
    print(f"\nDelivered to Telegram chat {config.TELEGRAM_CHAT_ID}")


def cmd_daily(args):
    """
    Evening note: what exited today, what to order tomorrow, and MTD ROI.

    Intended to run unattended from Windows Task Scheduler after the
    bhavcopy is published (~18:00-18:30 IST). Any failure is reported to
    Telegram rather than dying silently -- silence and "no trades today"
    must not look the same on your phone.
    """
    import alerts
    import daily_report

    as_of = (datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else date.today())
    try:
        report = daily_report.build(as_of)
        text = daily_report.render(report)
    except Exception as exc:
        logging.getLogger("momentum_tracker").exception("Daily report failed")
        alerts.send_failure(f"daily report for {as_of}", exc)
        raise

    print(text)

    # Record BEFORE sending: the note is the decision, delivery is just
    # transport. A Telegram outage must not erase the audit trail.
    import ledger
    ledger.record(report, rendered=text, kind="daily")

    if args.no_send:
        print("\n(--no-send: not delivered)")
        return
    if alerts.send(text):
        print(f"\nDelivered to Telegram chat {config.TELEGRAM_CHAT_ID}")
    else:
        print("\nDELIVERY FAILED -- see logs/app.log", file=sys.stderr)
        sys.exit(2)


def main():
    p = argparse.ArgumentParser(description="V4 momentum strategy")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("perf", help="portfolio performance note (expiry evening)")
    pf.add_argument("--no-send", action="store_true")
    pf.set_defaults(func=cmd_perf)

    h = sub.add_parser("history", help="look back at recorded monthly actions")
    h.add_argument("--trades", action="store_true",
                   help="list every closed trade instead of the monthly summary")
    h.set_defaults(func=cmd_history)

    sh = sub.add_parser("sheet", help="monthly order sheet, run on expiry evening")
    sh.add_argument("--expiry", required=True, help="YYYY-MM-DD (the expiry date)")
    sh.add_argument("--no-send", action="store_true")
    sh.set_defaults(func=cmd_sheet)

    d = sub.add_parser("daily", help="build and send the evening basket note")
    d.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    d.add_argument("--no-send", action="store_true",
                   help="print the note without sending it to Telegram")
    d.set_defaults(func=cmd_daily)

    b = sub.add_parser("basket", help="generate the basket for one expiry")
    b.add_argument("--expiry", required=True, help="YYYY-MM-DD (the expiry date)")
    b.set_defaults(func=cmd_basket)

    t = sub.add_parser("backtest", help="backtest a range of months")
    t.add_argument("--start", required=True, help="YYYY-MM")
    t.add_argument("--end", required=True, help="YYYY-MM")
    t.add_argument("--no-carry-forward", action="store_true",
                   help="disable v5 cross-month holding; force-sell every "
                        "month end and start each month from empty slots "
                        "(pre-v5 behaviour, for comparison)")
    t.set_defaults(func=cmd_backtest)

    args = p.parse_args()
    _setup_logging(args.verbose)
    try:
        args.func(args)
    except (strategy.StrategyError, nse_client.NseFetchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
