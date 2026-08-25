"""
CLI for the V4 strategy.

Generate the basket for the next month (run this the evening of expiry --
the bhavcopy is published after market close, and the orders go in at the
next 09:15 open):

    python run_strategy.py basket --expiry 2026-07-28

The backtest was REMOVED from this CLI on 02-Aug-2026. It is in
legacy/backtest_cmd.py, unreachable from anything scheduled.

Reason: it was the only caller that ran the strategy down a second code
path -- mechanical redeployment instead of the live LLM pick -- and a
second path is a second place for a bug to hide. Production now has one
path and one path only. The historical numbers it produced are recorded
in CONTEXT.md and do not need regenerating.
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

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
    """
    Score the universe as of an expiry date.

    Delegates to strategy.basket_for so this command cannot disagree with
    the evening note or the order sheet. It used to score independently
    and skip the surveillance veto entirely, so `run_strategy.py basket`
    printed ASM names that the basket actually sent to investors excluded.

    Returns (basket_table, full_ranking, hist, decision).
    """
    d = strategy.basket_for(expiry, symbols, sector_map)
    return d.table, d.full, d.hist, d


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
    # decision.table is already the POST-veto ten, backfills included.
    basket, _, _, decision = _snapshot(expiry, symbols, sectors)
    basket = basket.copy()
    basket["sector"] = basket["symbol"].map(lambda s: sectors.get(s, "Unclassified"))

    cols = ["rank", "symbol", "sector", "close", "stop_loss", "target",
            "weight_pct", "score", "volatility", "rollover", "cost_of_carry"]
    print(f"\nBasket from the {expiry} close\n")
    print(basket[cols].to_string(index=False,
                                 float_format=lambda v: f"{v:,.2f}"))
    if decision.veto_dropped:
        print("\nExcluded by surveillance:")
        for sym, why in decision.veto_dropped:
            print(f"  {sym} -- {why}")
        if decision.veto_added:
            print(f"Backfilled: {', '.join(decision.veto_added)}")
    if not decision.veto_ran:
        print("\nWARNING: the ASM feed did not answer -- this basket has NOT "
              "been surveillance-checked.")

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


def resolve_expiry(day: date) -> date:
    """
    The monthly F&O expiry for `day`'s month, holiday-adjusted.

    The raw rule is last Thursday (to Aug-2025) / last Tuesday (from
    Sep-2025). A future expiry cannot be checked against
    known_trading_days() -- that set is inferred from cached bhavcopy,
    which only covers the past -- so NSE's forward holiday feed is used
    and rolls the date BACK to the previous trading day.

    Falls back to the raw last-weekday if the feed is unreachable.
    """
    raw = strategy.expiry_for(day.year, day.month)
    try:
        import nse_corporate
        holidays = nse_corporate.fetch_trading_holidays()
    except Exception as exc:
        logging.getLogger("momentum_tracker").warning(
            "Holiday feed unavailable (%s); using raw last-weekday %s", exc, raw)
        return raw
    out, guard = raw, 0
    while (out in holidays or out.weekday() >= 5) and guard < 10:
        out -= timedelta(days=1)
        guard += 1
    return out


def _is_expiry(day: date) -> bool:
    try:
        return resolve_expiry(day) == day
    except strategy.StrategyError:
        return False


def _is_trading_day(day: date) -> bool:
    """
    True if NSE published a cash-market bhavcopy for `day`.

    Weekends are rejected without a network call. Otherwise we ask
    nse_client, which consults the disk cache first and the negative
    (.nodata) cache after that, so a known holiday costs nothing on
    repeat runs. Any error other than "no data" is treated as a trading
    day so a transient NSE outage still surfaces as a real failure alert
    rather than being silently swallowed.
    """
    if day.weekday() >= 5:
        return False
    try:
        nse_client.fetch_cm_bhavcopy(day)
        return True
    except nse_client.NseNoDataError:
        return False
    except nse_client.NseFetchError:
        return True


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

    if args.expiry:
        expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date()
    else:
        # Self-computing so the scheduled task never needs editing. NSE
        # moved monthly expiry to the last Tuesday from Sep-2025, and a
        # holiday rolls it BACK, so this is resolved against the actual
        # trading calendar rather than assumed.
        today = date.today()
        # The current month's expiry is in the FUTURE and therefore not in
        # the cached trading calendar, so the holiday roll-back cannot
        # resolve it. Seed the calendar with today (we only ever run this
        # on a trading evening) and fall back to the raw last-Tuesday if
        # it still cannot be placed.
        expiry = resolve_expiry(today)
        if expiry != today and not args.force:
            print(f"Today ({today}) is not the monthly expiry "
                  f"({expiry}) — nothing sent. Use --force to override.")
            return

    try:
        sheet = daily_report.build_entry_sheet(expiry)
        existing_text = daily_report.render_entry_sheet(sheet)
    except Exception as exc:
        logging.getLogger("momentum_tracker").exception("Entry sheet failed")
        alerts.send_failure(f"entry sheet for {expiry}", exc)
        raise

    # Expiry evening sends exactly THREE messages, in this order
    # (15-Aug-2026, explicit instruction -- "Why would there be a 4th
    # message. There are only 3 messages on expiry day: 1. Performance to
    # date  2. New Investors  3. Existing investors"):
    # Everything below, through the ledger writes, used to sit outside any
    # try/except -- a bug anywhere in here (25-Aug-2026: a Windows-only
    # strftime crash in render_new_investor_day0) died as an uncaught
    # exception with no log line past "Opened entry-tracking window", no
    # failure alert, and no messages sent, on the one evening silence is
    # least acceptable. Wrapped so any future failure here is at least
    # reported instead of vanishing.
    try:
        import ledger
        perf_data = ledger.performance()
        perf_text = daily_report.render_performance(perf_data)

        # Open the multi-day entry-tracking window over the FULL basket (every
        # row, not just the fresh buys) -- a new investor needs a fill plan
        # for EVERY name, including the ones tagged HOLD at the strategy
        # level (see build_entry_sheet's 'action' tag warning: HOLD there
        # means "continuing from last cycle's strategy basket", not "already
        # in this investor's book"). Those HOLD-tagged names are passed as
        # market_buy_symbols so they skip the 3-stage limit chain and fill at
        # Day-1's market open instead -- the same entry-price basis an
        # existing investor's TOP-UP gets (see open_window's and
        # render_new_investor_day0's docstrings for the full reasoning).
        # Fresh buys ALSO cover an existing investor's empty slots -- from
        # Day 1 onward the very same window and the very same entry_tracking.
        # render() message serve both audiences identically (locked in
        # earlier this thread: "Yes, this is correct, only Day 0 will be
        # different for new vs existing. Day 1 will be the same.").
        import entry_tracking
        full_basket_symbols = [r["symbol"] for r in sheet["rows"]]
        market_buy_symbols = [r["symbol"] for r in sheet["rows"] if r.get("action") == "HOLD"]
        # Per-symbol, not the flat config default -- build_entry_sheet already
        # resolved each row's own target_pct (LLM-derived when
        # config.LLM_TARGET_ENABLED, else the flat default per row anyway), so
        # passing the map keeps the Day-1/2 "Exit: Rs Y" notes in agreement
        # with the "Book at +X%" figure the Day-0 sheet just showed for that
        # same symbol (14-Aug-2026 fix -- previously nothing was passed here
        # and every follow-up note silently used the flat default regardless).
        target_pct_by_symbol = {r["symbol"]: r.get("target_pct", config.V4_TARGET_PCT)
                                for r in sheet["rows"]}
        et_state = entry_tracking.open_window(
            expiry, full_basket_symbols, stop_pct=sheet.get("stop_pct"),
            target_pct=target_pct_by_symbol,
            slot_target=sheet["sizing"].get("slot_target"),
            market_buy_symbols=market_buy_symbols)
        new_investor_text = entry_tracking.render_new_investor_day0(et_state)

        print(perf_text)
        print()
        print(new_investor_text)
        print()
        print(existing_text)

        # Record BEFORE sending -- same guarantee as the daily note (the
        # decision is what matters, delivery is just transport). One record
        # per message sent.
        buys = [r["symbol"] for r in sheet["rows"] if r.get("action") != "HOLD"]
        ledger.record_note("perf", expiry, rendered=perf_text, **perf_data)
        entry_tracking.record(et_state, new_investor_text)
        ledger.record_note(
            "sheet", expiry, rendered=existing_text,
            sells=[s["symbol"] for s in (sheet.get("sells") or [])],
            holds=list(sheet.get("holds") or []),
            buys=buys,
            stop_pct=sheet.get("stop_pct"),
            veto_dropped=[list(x) for x in (sheet.get("dropped") or [])],
            veto_ran=sheet.get("veto_ran"),
            rebalance=sheet.get("rebalance"),
        )

        # Apply the book side-effects of tonight's decisions. Both rest on the
        # same assumption as entry_tracking's fills: the investor is assumed to
        # follow every recommendation exactly, so the book is updated the
        # moment the decision is made, not on some later confirmation step
        # that doesn't exist in this system. (The TOP-UP names get a second,
        # more precise book.open_position() write on Day 1 once the actual
        # market-open fill price is known -- harmless double-write, same
        # target share count either way, just a more accurate entry_price
        # once real.)
        import book
        for s in (sheet.get("sells") or []):
            book.close_position(s["symbol"])
        for sym, d in (sheet.get("rebalance") or {}).items():
            if d.get("status") == "rebalance":
                book.adjust_shares(sym, d["new_shares"], expiry)
    except Exception as exc:
        logging.getLogger("momentum_tracker").exception(
            "New-investor/existing-investor delivery failed for %s", expiry)
        alerts.send_failure(f"sheet delivery for {expiry}", exc)
        raise

    if args.no_send:
        print("\n(--no-send: no messages delivered)")
        return

    ok_perf = alerts.send(perf_text)
    ok_new = alerts.send(new_investor_text) if et_state["stocks"] else True
    ok_existing = alerts.send(existing_text)
    if not (ok_perf and ok_new and ok_existing):
        print("\nDELIVERY FAILED -- see logs/app.log", file=sys.stderr)
        sys.exit(2)
    print(f"\nAll messages delivered to Telegram chat {config.TELEGRAM_CHAT_ID}")


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

    # Trading-day guard. On a weekend or exchange holiday NSE publishes no
    # bhavcopy, which is NOT a failure -- exit quietly rather than firing
    # an alarm every Saturday and training you to ignore alerts.
    if not args.force and not _is_trading_day(as_of):
        print(f"{as_of} is not a trading day (no bhavcopy) — nothing sent.")
        return

    # On expiry evening the `sheet` job sends the scorecard and next
    # month's orders. This note would be a redundant third message about
    # a basket that is being replaced tomorrow, so stand down.
    if not args.force and _is_expiry(as_of):
        print(f"{as_of} is the monthly expiry — the sheet job covers "
              f"tonight, nothing sent here.")
        return

    # Entry-tracking window: for the 1-3 sessions right after an expiry,
    # the basket isn't necessarily fully bought yet (see entry_tracking.py
    # -- the V5 multi-day fill chain). While that window is open, THIS
    # note is replaced by the tracking note, not sent alongside it, per
    # the agreed design. The evening a session's advance() resolves every
    # slot (filled or aborted), THAT SAME message already says "Final fill
    # list" -- there is nothing left to confirm, so mark_final_sent right
    # away and fall through to the normal note starting the very next
    # session (15-Aug-2026 fix: previously waited one extra "+1" evening
    # to repeat an already-final message unchanged -- pure noise once
    # resolution happens early, e.g. everything filled by Day 2 instead
    # of needing the Day-3 mandatory stage).
    import entry_tracking
    et_state = entry_tracking.load()
    if et_state is not None and entry_tracking.is_window_active(et_state):
        et_state = entry_tracking.advance(et_state, as_of)
        et_text = entry_tracking.render(et_state)
        print(et_text)
        entry_tracking.record(et_state, et_text)
        if et_state["resolved_as_of"] is not None:
            entry_tracking.mark_final_sent(et_state)
        if args.no_send:
            print("\n(--no-send: not delivered)")
            return
        if alerts.send(et_text):
            print(f"\nDelivered to Telegram chat {config.TELEGRAM_CHAT_ID}")
        else:
            print("\nDELIVERY FAILED -- see logs/app.log", file=sys.stderr)
            sys.exit(2)
        return

    # Built incrementally: stored state + one bhavcopy. The basket was
    # decided on expiry day and cannot change until the next one, so
    # re-ranking 208 symbols over 260 days every evening was recomputing a
    # fixed answer -- and that recomputation is where two of the
    # 03-Aug-2026 bugs lived. Re-running the same date applies zero
    # sessions; a run several days behind replays each missing session in
    # order. Falls back to a full rebuild if the state file is missing.
    try:
        import cycle_state
        report = cycle_state.build(as_of)
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

    # A stop, target, or momentum exit hit TODAY closes the position for
    # real -- book.py must drop it now, not wait for the next expiry's
    # SELL pass. Without this, a name that exits mid-month keeps a stale
    # share count in the book that the next HOLD-rebalance check would
    # wrongly measure drift against.
    import book
    for e in report.exits:
        if e.exit_date == as_of:
            book.close_position(e.symbol)

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
    sh.add_argument("--expiry", help="YYYY-MM-DD; omit to auto-detect today's expiry")
    sh.add_argument("--no-send", action="store_true")
    sh.add_argument("--force", action="store_true",
                    help="run even if today is not the monthly expiry")
    sh.set_defaults(func=cmd_sheet)

    d = sub.add_parser("daily", help="build and send the evening basket note")
    d.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    d.add_argument("--no-send", action="store_true",
                   help="print the note without sending it to Telegram")
    d.add_argument("--force", action="store_true",
                   help="run even if the date is not a trading day")
    d.set_defaults(func=cmd_daily)

    b = sub.add_parser("basket", help="generate the basket for one expiry")
    b.add_argument("--expiry", required=True, help="YYYY-MM-DD (the expiry date)")
    b.set_defaults(func=cmd_basket)



    args = p.parse_args()
    _setup_logging(args.verbose)
    try:
        args.func(args)
    except (strategy.StrategyError, nse_client.NseFetchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
