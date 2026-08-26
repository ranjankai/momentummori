"""
One-off: reconstruct the REAL day-by-day fill history (Day 1/2/3 proposed
price + filled or not, final entry) for all 10 names in the 28-Jul-2026
cycle, and write it into the real data/book.json.

WHY THIS EXISTS
----------------
book.json's 4 held-name records (TRENT, BANDHANBNK, GVT&D, POWERINDIA)
were originally approximated by research/book_backfill.py using the
EXPIRY-DAY close as a stand-in entry price -- never the real 3-stage
mechanism. The other 6 names (KAYNES, FORCEMOT, SAIL, IDEA, AMBER,
ADANIGREEN) had no book record at all. Per explicit instruction
(25-Aug-2026): every one of the 10 needs Day-1 proposed price + filled
y/n, Day-2 proposed + filled y/n, Day-3 proposed + filled y/n, and a
final entry price -- for the 4 holds, Day-1 proposed IS market open and
it always fills there (no limit chase for a top-up, see entry_tracking.
open_window's market_buy_symbols docstring).

METHOD
------
Does NOT reimplement the 3-stage chain. Replays the REAL production
functions -- entry_tracking.open_window() then advance() once per real
trading day -- against an isolated scratch state/book file, using the
actual cached bhavcopy for 29/30/31-Jul-2026 (the three real trading
days between the 28-Jul expiry and the next month). Those functions
already build the fill_history dict (25-Aug-2026 addition) and call
book.open_position(..., fill_history=...) at the moment each name
actually fills -- this script only decides WHICH symbols are market_buy
(the 4 continuing holds) and transplants the scratch book's resulting
records into the real book.json afterward.
"""
import datetime as dt
import json
import sys

sys.path.insert(0, ".")
import book                                                    # noqa: E402
import entry_tracking as et                                    # noqa: E402

SCRATCH_BOOK = "/tmp/book_jul28_fillhistory.json"
SCRATCH_STATE = "/tmp/entry_tracking_jul28_fillhistory.json"

EXPIRY = dt.date(2026, 7, 28)
BASKET = ["TRENT", "KAYNES", "BANDHANBNK", "GVT&D", "POWERINDIA",
          "FORCEMOT", "SAIL", "IDEA", "AMBER", "ADANIGREEN"]
HELD = {"TRENT", "BANDHANBNK", "GVT&D", "POWERINDIA"}  # continuing from Jun-Jul cycle
STOP_PCT = 5.0
TARGET_PCT = 40.0
REAL_TRADING_DAYS = [dt.date(2026, 7, 29), dt.date(2026, 7, 30), dt.date(2026, 7, 31)]

# Wipe any stale scratch files from a prior run of this script.
import os
for f in (SCRATCH_BOOK, SCRATCH_STATE):
    if os.path.exists(f):
        os.remove(f)

# book.seed_pending is called internally by open_window -- point it at
# the scratch book, not the real one, for this whole replay.
book.BOOK_FILE = SCRATCH_BOOK

state = et.open_window(EXPIRY, BASKET, STOP_PCT, target_pct=TARGET_PCT,
                       path=SCRATCH_STATE, market_buy_symbols=HELD)

print("=== Day 0 (expiry evening) quotes ===")
for sym, d in state["stocks"].items():
    tag = " [HOLD/market_buy]" if d.get("market_buy") else ""
    print(f"  {sym}: quote={d.get('quote_price')} shares={d.get('shares')}{tag}")

for day in REAL_TRADING_DAYS:
    state = et.advance(state, day, path=SCRATCH_STATE)
    print(f"\n=== after {day} (session {len(state['sessions'])}) ===")
    for sym, d in state["stocks"].items():
        print(f"  {sym}: status={d['status']} filled_day={d.get('filled_day')} "
             f"price={d.get('price')} history={d.get('history')}")

print("\n=== Final resolved_as_of:", state["resolved_as_of"], "===")

scratch_book = book.load(SCRATCH_BOOK)
print("\n=== Scratch book (to be transplanted into real book.json) ===")
for sym in BASKET:
    print(f"  {sym}: {scratch_book.get(sym)}")

# Sanity: every name should have resolved to a real position (none of the
# 10 were expected to abort this cycle -- cycle_state.json shows all 10
# as HOLD/EXITED, never "never entered").
missing = [s for s in BASKET if s not in scratch_book]
if missing:
    print(f"\n!! WARNING: {missing} never resolved to a book position -- "
          f"inspect before transplanting.")
else:
    real_book = book.load("data/book.json")
    for sym in BASKET:
        real_book[sym] = scratch_book[sym]
    book.save(real_book, "data/book.json")
    print("\nTransplanted all 10 records into data/book.json.")
