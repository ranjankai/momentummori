"""
One-off (26-Aug-2026): replay the REAL Day-0 (expiry-evening) message
generation for the 25-Aug-2026 expiry, using the ACTUAL production
functions (daily_report.build_entry_sheet, entry_tracking.open_window,
entry_tracking.render_new_investor_day0) against a scratch copy of the
real data restored to its state just BEFORE cmd_sheet ran -- i.e. before
today's rebase overwrote book.json. This is what should have gone out
on 25-Aug evening, with tonight's fixes applied, produced by calling the
real code rather than hand-computing it.
"""
import logging
import sys
from datetime import date

sys.path.insert(0, ".")
logging.disable(logging.CRITICAL)

import book
import config
import daily_report
import entry_tracking

SCRATCH = "/tmp/day0_replay"
book.BOOK_FILE = f"{SCRATCH}/book.json"
book.ARCHIVE_FILE = f"{SCRATCH}/book_archive.jsonl"
entry_tracking.STATE_FILE = f"{SCRATCH}/entry_tracking.json"

expiry = date(2026, 8, 25)

sheet = daily_report.build_entry_sheet(expiry)
existing_text = daily_report.render_entry_sheet(sheet)

full_basket_symbols = [r["symbol"] for r in sheet["rows"]]
market_buy_symbols = [r["symbol"] for r in sheet["rows"] if r.get("action") == "HOLD"]
target_pct_by_symbol = {r["symbol"]: r.get("target_pct", config.V4_TARGET_PCT)
                        for r in sheet["rows"]}

et_state = entry_tracking.open_window(
    expiry, full_basket_symbols, stop_pct=sheet.get("stop_pct"),
    target_pct=target_pct_by_symbol,
    slot_target=sheet["sizing"].get("slot_target"),
    market_buy_symbols=market_buy_symbols)
new_investor_text = entry_tracking.render_new_investor_day0(et_state)

import ledger
perf_data = ledger.performance()
perf_text = daily_report.render_performance(perf_data)

print("=" * 70)
print("MESSAGE 1 OF 3 -- PERFORMANCE TO DATE")
print("=" * 70)
print(perf_text)
print()
print("=" * 70)
print("MESSAGE 2 OF 3 -- NEW INVESTORS (Day 0)")
print("=" * 70)
print(new_investor_text)
print()
print("=" * 70)
print("MESSAGE 3 OF 3 -- EXISTING INVESTORS (Day 0 entry sheet)")
print("=" * 70)
print(existing_text)
