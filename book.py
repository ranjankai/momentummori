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
- entry_tracking.py, the moment a slot resolves FILLED (Day 1 or Day 2):
  open_position() -- adds the symbol at its actual fill shares/price/date.
- daily_report.py, at the next expiry, for every name that drops out of
  the basket: close_position() -- removes the symbol (SELL, no partial
  holding carries forward).
- daily_report.py's HOLD rebalance step, when a name's weight has drifted
  outside the band and a trim/top-up is recommended: adjust_shares() --
  updates the share count in place, on the same followed-the-reco
  assumption, applied consistently across the book's life.

PERSISTENCE
------------
Same atomic-write JSON pattern as cycle_state.py / entry_tracking.py.
"""

import json
import logging
import os
from datetime import date

import config

logger = logging.getLogger("momentum_tracker.book")

BOOK_FILE = os.path.join(config.DATA_DIR, "book.json")


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
                   origin_expiry: date, path: str = None) -> dict:
    """Called by entry_tracking.py the moment a slot actually fills."""
    book = load(path)
    book[symbol] = {
        "shares": int(shares),
        "entry_price": round(float(price), 2),
        "entry_date": str(fill_date),
        "origin_expiry": str(origin_expiry),
    }
    save(book, path)
    logger.info("Book: opened %s, %d sh @ %.2f (%s)", symbol, shares, price, fill_date)
    return book


def close_position(symbol: str, path: str = None) -> dict:
    """Called when a name drops out of the basket at expiry (SELL)."""
    book = load(path)
    if symbol in book:
        book.pop(symbol, None)
        save(book, path)
        logger.info("Book: closed %s", symbol)
    return book


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
