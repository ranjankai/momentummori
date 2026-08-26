"""
Persistent per-symbol share book -- the model portfolio's actual holdings,
month over month.

WHY THIS EXISTS
----------------
strategy.Position (the object the whole system reconstructs from bhavcopy
every run) carries entry price, stop, target and current price -- but
never a share count. Share counts are only ever computed once, as a
RECOMMENDATION, when an entry sheet or entry-tracking note is rendered;
nothing persisted them forward. That's fine for a position's first month
(the recommendation IS the assumed holding), but breaks the moment a
position survives into a second month: telling a HELD name it has
drifted off target weight requires comparing what's actually held today
against today's slot target, and "actually held" doesn't exist anywhere
without this file.

ASSUMPTION (agreed 13-Aug-2026): every investor is assumed to have
followed the minimum-filled-basket recommendation exactly, and Jul-Aug
'26 is month 1 of live operation -- every name entering the book from
here on originates from a real entry_tracking fill, so there is no
before-the-beginning history to reconstruct or guess at.

WHAT WRITES TO THE BOOK
------------------------
- entry_tracking.py, at expiry evening, for every name in the new basket
  not already a real position: seed_pending() -- a blank placeholder row
  so the book has all 10 names from Day 0, not just whichever happen to
  need a rebalance check.
- entry_tracking.py, the moment a slot resolves FILLED (Day 1, 2, or 3):
  open_position() -- overwrites the pending row with actual fill
  shares/price/date/risk_anchor.
- run_strategy.py, at the next expiry (SELL) or on a real mid-month
  stop/target/rollover exit: close_position() -- archives the full
  record to book_archive.jsonl, then removes it from the live book.
- daily_report.py's HOLD rebalance step, when a name's weight has drifted
  outside the band and a trim/top-up is recommended: adjust_shares() --
  updates the share count in place, on the same followed-the-reco
  assumption, applied consistently across the book's life.

book.json IS THE SINGLE SOURCE OF TRUTH for what was actually paid (25-
Aug-2026 fix): cycle_state.py's live P&L tracking and daily_report.
build()'s outgoing-month reconstruction previously each independently
assumed every position entered at "Day-1's open" -- ignoring this file
entirely, even though it already had the real answer. Both now read
entry_price/risk_anchor from here (via strategy.simulate_month's
entry_overrides parameter) instead of re-deriving their own idealized
version. Real money real-fill data lives in exactly one place.

PERSISTENCE
------------
Same atomic-write JSON pattern as cycle_state.py / entry_tracking.py.
book_archive.jsonl is append-only, one JSON line per closed position.
"""

import json
import logging
import os
from datetime import date, datetime

import config

logger = logging.getLogger("momentum_tracker.book")

BOOK_FILE = os.path.join(config.DATA_DIR, "book.json")
ARCHIVE_FILE = os.path.join(config.DATA_DIR, "book_archive.jsonl")


def load(path: str = None) -> dict:
    path = path or BOOK_FILE
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(book: dict, path: str = None) -> None:
    path = path or BOOK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(book, fh, indent=2, default=str)
    os.replace(tmp, path)


def open_position(symbol: str, shares: int, price: float, fill_date: date,
                   origin_expiry: date, risk_anchor: float = None,
                   fill_history: dict = None, target_pct: float = None,
                   stop_pct: float = None, path: str = None) -> dict:
    """
    Called by entry_tracking.py the moment a slot actually fills.

    `target_pct`/`stop_pct` (26-Aug-2026 addition): the PER-SYMBOL
    percentages this fill was actually quoted against -- entry_tracking.py
    already resolves a per-symbol target when config.LLM_TARGET_ENABLED is
    on (each row's own "Book at +X%"), but book.json never stored it, so
    cycle_state.py's daily walk had to re-derive stop/target from its own
    flat, window-level percentages instead of the real committed ones.
    Harmless today only because LLM_TARGET_ENABLED is off, so every symbol
    shares one target and the two happen to agree -- the moment that flag
    flips on, the flat re-derivation would silently diverge from what the
    investor was actually told. Optional so older callers/backfills that
    don't have a per-symbol figure still work; cycle_state.py falls back
    to its own flat percentages when a row doesn't carry these.

    `risk_anchor` (25-Aug-2026 addition): a Day-2/3 fill's actual price is
    NOT what stop/target are computed off -- entry_tracking.py has always
    anchored those to Day-1's real market open, on purpose (a late, higher
    fill must not drag the stop up with it; see entry_tracking.py's
    advance() n==1 branch). Book previously only stored the fill price,
    so anything reading the book for stop/target basis (there was
    nothing, until now -- see cycle_state.py's 25-Aug-2026 fix) would
    have used the WRONG basis for any name that filled on Day 2 or 3.
    Defaults to `price` when not given (correct for a same-day, Day-1
    fill, where fill price and anchor are identical anyway).

    `fill_history` (25-Aug-2026 addition): the day-by-day record of what
    was actually proposed and whether it filled -- {"day1": {"date",
    "proposed_price", "filled", ...}, "day2": {...}, "day3": {...}} --
    built up by entry_tracking.py's advance() as it walks the real 3-stage
    chain. Optional so older/simpler callers (tests, one-off backfills
    that only know the final fill) still work without it; a position with
    no history is not wrong, just less detailed than one entry_tracking.py
    walked live.
    """
    book = load(path)
    record = {
        "shares": int(shares),
        "entry_price": round(float(price), 2),
        "entry_date": str(fill_date),
        "origin_expiry": str(origin_expiry),
        "risk_anchor": round(float(risk_anchor if risk_anchor is not None else price), 2),
    }
    if fill_history is not None:
        record["fill_history"] = fill_history
    if target_pct is not None:
        record["target_pct"] = float(target_pct)
    if stop_pct is not None:
        record["stop_pct"] = float(stop_pct)
    book[symbol] = record
    save(book, path)
    logger.info("Book: opened %s, %d sh @ %.2f (%s)", symbol, shares, price, fill_date)
    return book


def seed_pending(symbols, origin_expiry: date, path: str = None) -> dict:
    """
    Called once, at expiry evening, for every name in the NEW basket that
    isn't already a real position (25-Aug-2026 addition -- "book.json
    should have 10 names with blank entry prices on Day of expiry
    evening", explicit instruction). Gives the book a complete row for
    the month from the start instead of only gaining entries lazily as
    fills happen over the next 1-3 days -- open_position() below
    overwrites a pending row with the real fill exactly like any other.
    Never touches a symbol that's already a real (or already-pending)
    position -- a continuing HOLD keeps its real historical entry.
    """
    book = load(path)
    changed = False
    for sym in symbols:
        if sym not in book:
            book[sym] = {
                "shares": 0, "entry_price": None, "entry_date": None,
                "origin_expiry": str(origin_expiry), "risk_anchor": None,
                "status": "pending",
            }
            changed = True
    if changed:
        save(book, path)
    return book


def _archive_path(path: str = None) -> str:
    return os.path.join(os.path.dirname(path or BOOK_FILE),
                        os.path.basename(ARCHIVE_FILE)) if path else ARCHIVE_FILE


def _append_archive(record: dict, path: str = None) -> None:
    archive_path = _archive_path(path)
    try:
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with open(archive_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.error("Could not append to archive %s: %s", record.get("symbol"), exc)


def close_position(symbol: str, exit_price: float = None, exit_date: date = None,
                    reason: str = None, path: str = None) -> dict:
    """
    Called when a name drops out of the basket at expiry (SELL) or exits
    mid-month (stop/target/rollover) -- i.e. a REAL position, always
    archived with kind="actual" (26-Aug-2026: see `kind` on
    `write_simulated_record` for the other half of this distinction).

    25-Aug-2026 fix: previously just deleted the record, so there was no
    permanent history of any real entry/exit ever -- every later question
    about "what actually happened last month" had to re-simulate from raw
    price history instead of reading a real answer, including things a
    re-simulation can never recover (e.g. ASM surveillance, which NSE
    only publishes as a current snapshot). Now archives the full record
    to book_archive.jsonl (append-only) before removing it from the live
    book. `exit_price`/`exit_date`/`reason` are optional so existing
    callers that don't have them yet don't break -- an archived row with
    those blank is still strictly better than no row at all.
    """
    book = load(path)
    if symbol in book:
        record = dict(book[symbol])
        record.update({
            "symbol": symbol,
            "kind": "actual",
            "exit_price": round(float(exit_price), 2) if exit_price is not None else None,
            "exit_date": str(exit_date) if exit_date is not None else None,
            "reason": reason,
            "closed_at": datetime.now().isoformat(timespec="seconds"),
        })
        _append_archive(record, path)
        book.pop(symbol, None)
        save(book, path)
        logger.info("Book: closed %s (archived)", symbol)
    return book


def write_simulated_record(record: dict, path: str = None) -> None:
    """
    Append ONE backtest-derived (never real-money) record to the SAME
    book_archive.jsonl, tagged kind="simulated" (26-Aug-2026 addition --
    negotiated explicitly against a two-file proposal: "why create two
    archives, you can simply add a S and A notation to the header").

    Required keys on `record`: symbol, origin_expiry, backtest_version,
    entry_price, entry_date, risk_anchor, fill_history, exit_price,
    exit_date, reason. `fill_history` must be the same day1/day2/day3
    shape open_position() stores, so "how was this entry price derived"
    is fully readable from the archive alone -- the whole point of
    writing this once per (symbol, cycle, version) is that nothing here
    is ever re-simulated to answer that question again.

    Any reader used for LIVE investor-facing purposes (currently only
    holdings_for_expiry, called once a month by
    daily_report.build_entry_sheet) MUST filter to kind == "actual" --
    a simulated record is a backtest data point, never something an
    investor actually held.
    """
    record = dict(record)
    record["kind"] = "simulated"
    record.setdefault("closed_at", datetime.now().isoformat(timespec="seconds"))
    _append_archive(record, path)
    logger.info("Book: archived simulated %s (%s, %s)",
               record.get("symbol"), record.get("origin_expiry"),
               record.get("backtest_version"))


def adjust_shares(symbol: str, new_shares: int, rebalance_date: date,
                   path: str = None) -> dict:
    """
    Called after a HOLD rebalance recommendation is sent. Updates the share
    count in place -- entry_price/entry_date/origin_expiry (cost basis) are
    untouched, since a trim/top-up is not a fresh entry.
    """
    book = load(path)
    if symbol in book:
        old = book[symbol]["shares"]
        book[symbol]["shares"] = int(new_shares)
        book[symbol].setdefault("rebalance_history", []).append(
            {"date": str(rebalance_date), "from_shares": old, "to_shares": int(new_shares)})
        save(book, path)
        logger.info("Book: rebalanced %s, %d -> %d sh (%s)",
                   symbol, old, new_shares, rebalance_date)
    return book


def get(symbol: str, path: str = None) -> dict:
    return load(path).get(symbol)


def holdings_for_expiry(expiry, path: str = None) -> dict:
    """
    Every REAL position (live or already archived) that originated in
    the cycle governed by `expiry` -- i.e. the ground truth for "what
    was actually held during this outgoing month", keyed by symbol.

    26-Aug-2026 addition, the structural fix for a real bug: daily_report.
    build_entry_sheet used to answer "what's currently held" by asking
    daily_report.build() to RE-DERIVE the whole outgoing month's basket
    from scratch (re-running selection/ranking against today's data) --
    which is not "what was actually held", it's "what the ranking
    algorithm would produce if run again today". Those differ whenever
    something un-reconstructable changed a decision after the fact: ASM
    surveillance is only ever a CURRENT snapshot, so a name vetoed a
    month ago (KALYANKJIL, 03-Aug) silently reappears in the redo, and a
    name that was actually real (ADANIGREEN) silently vanishes. book.json
    (plus its archive) is the one place this was ever recorded WITHOUT
    needing to be re-derived -- it's written the moment a real fill or a
    real close happens, so there is nothing to reconstruct. Merges live
    (still held) and archived (already sold/stopped/targeted this same
    cycle) records; live wins on the rare chance a symbol appears in
    both.

    26-Aug-2026: filters archive rows to kind == "actual" (a row with no
    "kind" at all -- every record written before this field existed --
    is treated as actual, since every archive entry until tonight was a
    real position). This is the ONE live-investor-facing reader of
    book_archive.jsonl (via daily_report.build_entry_sheet, once a month
    at expiry); a kind="simulated" backtest row must never leak into an
    actual investor's held-basket reconstruction.
    """
    expiry = str(expiry)
    out = {}
    archive_path = os.path.join(os.path.dirname(path or BOOK_FILE),
                                os.path.basename(ARCHIVE_FILE)) if path else ARCHIVE_FILE
    if os.path.exists(archive_path):
        with open(archive_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind", "actual") != "actual":
                    continue
                if rec.get("origin_expiry") == expiry and rec.get("symbol"):
                    out[rec["symbol"]] = rec
    for sym, pos in load(path).items():
        if pos.get("origin_expiry") == expiry and pos.get("entry_price") is not None:
            out[sym] = dict(pos, symbol=sym)
    return out


def get_archived(symbol: str, path: str = None) -> dict:
    """
    Latest archived record for `symbol`, or None.

    25-Aug-2026 addition: a position that exited mid-cycle (stop/target)
    is archived and removed from the live book by close_position() --
    correct for the LIVE book, but it means get(symbol) goes silently
    blank the moment daily_report.build() needs to reconstruct an
    OUTGOING month that included that exit (e.g. the expiry-evening
    scorecard). Without this fallback, entry_overrides quietly drops the
    real entry for anything that already closed, and the reconstruction
    falls back to the idealized Day-1-open guess for exactly the kind of
    position this file exists to get right. A symbol can appear more than
    once across cycles (re-entered later); the LAST line wins, matching
    how the live book always reflects the most recent state.

    26-Aug-2026: skips kind=="simulated" rows for the same reason
    holdings_for_expiry does -- this is a live-reporting fallback and
    must never surface a backtest row as if it were a real fill.
    """
    path = os.path.join(os.path.dirname(path or BOOK_FILE),
                        os.path.basename(ARCHIVE_FILE)) if path else ARCHIVE_FILE
    if not os.path.exists(path):
        return None
    found = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind", "actual") != "actual":
                continue
            if rec.get("symbol") == symbol:
                found = rec
    return found


def simulated_records(backtest_version: str = None, symbol: str = None,
                      path: str = None) -> list:
    """
    All kind=="simulated" rows from book_archive.jsonl, optionally
    filtered to one `backtest_version` and/or one `symbol` (26-Aug-2026
    addition -- the read side of write_simulated_record). This is what a
    future backtest run for the CURRENT version should check first: if a
    (symbol, origin_expiry) pair is already here under the current
    version, it must be read, never re-simulated -- "I DON'T WANT ANY
    RECREATION OF ANY DATA AGAIN IN A PARTICULAR VERSION FOR
    BACKTESTING."
    """
    archive_path = _archive_path(path)
    out = []
    if not os.path.exists(archive_path):
        return out
    with open(archive_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "simulated":
                continue
            if backtest_version is not None and rec.get("backtest_version") != backtest_version:
                continue
            if symbol is not None and rec.get("symbol") != symbol:
                continue
            out.append(rec)
    return out


def performance(path: str = None) -> dict:
    """
    Portfolio performance to date, for the expiry-evening message.

    26-Aug-2026: replaces ledger.performance()/monthly_summary(), which
    read a single mtd_return_pct FROZEN into ledger.jsonl once per day and
    never revisited. That's exactly why a real correction to
    book_archive.jsonl earlier tonight (the REBASE backfill) never showed
    up in what investors were actually told -- nothing ever re-derived the
    number after the correction, so the stale pre-correction figure just
    sat there being sent out. Per explicit instruction, ledger.jsonl is
    audit-only from here forward -- a permanent record of what was SAID,
    for checking later whether the notes were right -- and nothing in the
    live pipeline reads it for a current answer any more. This function
    computes the return fresh, every call, straight from book_archive.jsonl
    (closed, kind=="actual" records) plus book.json's still-open positions
    marked to today's last_close via cycle_state.json's live cursor. A
    correction to the archive is reflected the very next time this runs --
    no separate step to remember, no second copy to go stale.

    Same two cumulative conventions as before:
      absolute_sum  -- monthly returns ADDED (matches the convention
                       CONTEXT.md's quoted track record uses).
      absolute_comp -- monthly returns COMPOUNDED, the only valid base for
                       CAGR.

    Per-slot convention matches cycle_state.py's to_report() exactly:
    return_pct for a month = (sum of every filled/closed slot's pnl_pct)
    / config.PORTFOLIO_SIZE -- an empty/aborted slot contributes 0 to the
    sum but still counts in the denominator, same as before.
    """
    import cycle_state as _cycle_state

    slots = getattr(config, "PORTFOLIO_SIZE", 10)
    by_expiry = {}

    archive_path = _archive_path(path)
    if os.path.exists(archive_path):
        with open(archive_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind", "actual") != "actual":
                    continue
                expiry = rec.get("origin_expiry")
                entry = rec.get("entry_price")
                exit_px = rec.get("exit_price")
                if not expiry or not entry or exit_px is None:
                    continue
                pnl = (exit_px - entry) / entry * 100.0
                grp = by_expiry.setdefault(
                    expiry, {"pnl": [], "exits": 0, "wins": 0, "last_run": expiry})
                grp["pnl"].append(pnl)
                grp["exits"] += 1
                if pnl > 0:
                    grp["wins"] += 1
                exit_date = rec.get("exit_date")
                if exit_date and exit_date > grp["last_run"]:
                    grp["last_run"] = exit_date

    # Still-open positions: mark to market via cycle_state.json's live
    # last_close cursor -- legitimate, non-duplicated data (today's price,
    # not a copy of entry/stop/target), and the only file that tracks it.
    # cycle_state.json only ever holds the CURRENT cycle (it's fully
    # overwritten at every open_cycle()), so this only applies to that one
    # expiry; anything else with no live price marks at 0% rather than
    # guessing.
    cs = _cycle_state.load()
    cs_expiry = cs.get("expiry") if cs else None
    last_close_by_sym = {}
    if cs:
        for sym, pos in cs.get("positions", {}).items():
            if pos.get("last_close"):
                last_close_by_sym[sym] = pos["last_close"]

    open_count_by_expiry = {}
    for sym, pos in load(path).items():
        entry = pos.get("entry_price")
        expiry = pos.get("origin_expiry")
        if entry is None or not expiry:
            continue
        current = last_close_by_sym.get(sym) if expiry == cs_expiry else None
        pnl = ((current - entry) / entry * 100.0) if current else 0.0
        grp = by_expiry.setdefault(
            expiry, {"pnl": [], "exits": 0, "wins": 0, "last_run": expiry})
        grp["pnl"].append(pnl)
        open_count_by_expiry[expiry] = open_count_by_expiry.get(expiry, 0) + 1

    rows = []
    for expiry in sorted(by_expiry):
        grp = by_expiry[expiry]
        return_pct = sum(grp["pnl"]) / slots if slots else 0.0
        rows.append({
            "expiry": expiry,
            "last_run": grp["last_run"],
            "return_pct": return_pct,
            "closed": grp["exits"],
            "wins": grp["wins"],
            "win_rate": round(grp["wins"] / grp["exits"] * 100, 1) if grp["exits"] else None,
            "open": open_count_by_expiry.get(expiry, 0),
        })

    if not rows:
        return {"months": [], "absolute_sum": 0.0, "absolute_comp": 0.0,
                "cagr": None, "n_months": 0, "extrapolated": True}

    comp = 1.0
    for r in rows:
        comp *= (1 + r["return_pct"] / 100.0)
    absolute_comp = (comp - 1) * 100.0
    absolute_sum = sum(r["return_pct"] for r in rows)
    n = len(rows)
    cagr = ((comp ** (12.0 / n)) - 1) * 100.0 if comp > 0 and n > 0 else None

    return {
        "months": rows,
        "absolute_sum": absolute_sum,
        "absolute_comp": absolute_comp,
        "cagr": cagr,
        "n_months": n,
        "extrapolated": n < 12,
    }
