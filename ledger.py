"""
Append-only record of what the system told you to do.

WHY
---
Everything else in this repo is regenerable from bhavcopy. This is not.
It records what the strategy said on the day it said it -- before the
outcome was known -- which is the only way to check later whether you
actually followed it, and whether the notes were right.

Two artefacts per run:
  data/ledger.jsonl        one JSON record per run, machine-readable
  data/notes/YYYY-MM-DD.txt  the exact message that went to Telegram

Append-only by design. Records are never rewritten: a corrected view of
the past is not a record of the past. Re-running the same date appends a
second record rather than replacing the first, and `history()` returns
the latest per date while keeping the earlier ones visible on disk.

Writes are best-effort. A ledger failure logs loudly but never aborts a
run -- losing the audit trail is bad, losing the trading note is worse.
"""

import json
import logging
import os
from datetime import date, datetime

import config

logger = logging.getLogger("momentum_tracker.ledger")


def _serialise(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def record(rpt, rendered: str = None, kind: str = "daily") -> bool:
    """
    Append one run to the ledger. Returns True on success.

    `rpt` is a daily_report.Report. `rendered` is the exact text sent.
    """
    if not config.LEDGER_ENABLED:
        return False

    entry = {
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "as_of": _serialise(rpt.as_of),
        "expiry": _serialise(rpt.expiry),
        "entry_date": _serialise(rpt.entry_date),
        "mtd_return_pct": round(rpt.mtd_return_pct, 4),
        "exits": [
            {
                "symbol": e.symbol,
                "reason": e.reason,
                "entry": round(e.entry, 2),
                "exit": round(e.exit_px, 2),
                "pnl_pct": round(e.pnl_pct, 4),
                "exit_date": _serialise(e.exit_date),
            }
            for e in rpt.exits
        ],
        "sell_orders": list(getattr(rpt, "sell_orders", []) or []),
        "buy_orders": list(getattr(rpt, "buy_orders", []) or []),
        "holdings": [
            {
                "symbol": h.symbol,
                "entry": round(h.entry, 2),
                "entry_date": _serialise(h.entry_date),
                "last": round(h.last, 2) if h.last else None,
                "stop": round(h.stop, 2),
                "target": round(h.target, 2),
                "pnl_pct": round(h.pnl_pct, 4),
                "target_placeable": h.target_placeable,
            }
            for h in rpt.holdings
        ],
        "empty_slots": rpt.empty_slots,
        "veto_dropped": [list(x) for x in (rpt.veto_dropped or [])],
        "veto_ran": rpt.veto_ran,
        "flagged_actions": rpt.flagged_actions or [],
    }

    ok = True
    try:
        os.makedirs(os.path.dirname(config.LEDGER_FILE), exist_ok=True)
        with open(config.LEDGER_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=_serialise) + "\n")
        logger.info("Ledger updated for %s (%s)", entry["as_of"], kind)
    except OSError as exc:
        logger.error("Could not append to ledger %s: %s", config.LEDGER_FILE, exc)
        ok = False

    if rendered:
        try:
            os.makedirs(config.LEDGER_ARCHIVE_DIR, exist_ok=True)
            path = os.path.join(config.LEDGER_ARCHIVE_DIR,
                                f"{entry['as_of']}_{kind}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(rendered)
        except OSError as exc:
            logger.error("Could not archive note: %s", exc)
            ok = False
    return ok


def history(kind: str = None) -> list:
    """
    Every ledger record, oldest first. Where a date was run more than
    once, only the LAST record for that date is returned -- but nothing
    is deleted from disk.
    """
    if not os.path.exists(config.LEDGER_FILE):
        return []
    latest = {}
    try:
        with open(config.LEDGER_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    logger.warning("Skipping unparseable ledger line")
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                latest[(rec.get("as_of"), rec.get("kind"))] = rec
    except OSError as exc:
        logger.error("Could not read ledger: %s", exc)
        return []
    return [latest[k] for k in sorted(latest)]


def closed_trades() -> list:
    """Every exit ever recorded, de-duplicated on (symbol, exit_date)."""
    seen, out = set(), []
    for rec in history():
        for e in rec.get("exits", []):
            key = (e["symbol"], e["exit_date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    return sorted(out, key=lambda e: e["exit_date"])


def performance() -> dict:
    """
    Portfolio performance to date, for the expiry-evening message.

    Returns per-month returns plus two cumulative figures:

      absolute_sum  -- monthly returns ADDED. This is the convention
                       strategy.simulate_month uses and the one the
                       +32.34% table in CONTEXT.md is quoted on, so it is
                       the comparable number.
      absolute_comp -- monthly returns COMPOUNDED. This is what your
                       capital actually did, and the only valid base for
                       an annualised figure.

    CAGR is derived from absolute_comp. Below 12 months it is an
    extrapolation, not a track record: `extrapolated` says so.
    """
    rows = monthly_summary()
    if not rows:
        return {"months": [], "absolute_sum": 0.0, "absolute_comp": 0.0,
                "cagr": None, "n_months": 0, "extrapolated": True}

    comp = 1.0
    for r in rows:
        comp *= (1 + r["return_pct"] / 100.0)
    absolute_comp = (comp - 1) * 100.0
    absolute_sum = sum(r["return_pct"] for r in rows)

    n = len(rows)
    cagr = None
    if comp > 0 and n > 0:
        cagr = ((comp ** (12.0 / n)) - 1) * 100.0

    return {
        "months": rows,
        "absolute_sum": absolute_sum,
        "absolute_comp": absolute_comp,
        "cagr": cagr,
        "n_months": n,
        "extrapolated": n < 12,
    }


def monthly_summary() -> list:
    """
    One row per expiry month: last recorded MTD, trade count, win rate.
    This is the look-back table.
    """
    by_month = {}
    for rec in history(kind="daily"):
        by_month[rec.get("expiry")] = rec
    rows = []
    for expiry in sorted(by_month):
        rec = by_month[expiry]
        exits = rec.get("exits", [])
        wins = sum(1 for e in exits if e["pnl_pct"] > 0)
        rows.append({
            "expiry": expiry,
            "last_run": rec["as_of"],
            "return_pct": rec["mtd_return_pct"],
            "closed": len(exits),
            "wins": wins,
            "win_rate": round(wins / len(exits) * 100, 1) if exits else None,
            "open": len(rec.get("holdings", [])),
        })
    return rows
