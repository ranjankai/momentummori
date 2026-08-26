"""
One-off: reconstruct book.json's missing share-count history for the 4
continuing (HOLD) names in tonight's real 25-Aug-2026 sheet -- TRENT,
BANDHANBNK, POWERINDIA, GVT&D -- none of which have a book record because
book.py didn't exist when the 28-Jul-2026 cycle actually opened.

Per explicit instruction: "assume you started as a new investor in
Mar-Apr cycle and accordingly build the existing investor portfolio."
Walks the SAME mechanical chain build_entry_sheet uses every real month
(_compute_min_portfolio_sizing -> coverage-scale k -> _resolve_shares_to_
target -> _compute_hold_rebalance), cycle by cycle, using the real
production functions (not a reimplementation), real historical price
data for each expiry's entry bands, and each cycle's REAL basket where
known.

CAVEAT, stated plainly: baskets for Mar/Apr/May/Jun-2026 expiries come
from harness.v4_basket, which does NOT apply the ASM surveillance veto
(NSE publishes ASM only as a current snapshot -- no historical archive
exists, so a historically-accurate veto is not reconstructable; see
harness.v4_basket's own docstring). The FINAL step (28-Jul-2026 expiry)
uses the REAL, known basket instead of a backtest, since that one is
ground truth from this session's earlier reconstruction of the actual
production entry_tracking.json. Where the no-veto chain and the real
July basket disagree on which names were actually continuously held
(BANDHANBNK, POWERINDIA), this script cannot verify a multi-cycle
history and falls back to treating that name as a fresh 29-Jul-2026
entry -- explicitly flagged in the output, not silently guessed.
"""
import datetime as dt
import json
import sys

sys.path.insert(0, "..")
import strategy                                              # noqa: E402
import daily_report                                           # noqa: E402
import book                                                    # noqa: E402

BOOK_SCRATCH = "/tmp/book_backfill.json"
book.BOOK_FILE = BOOK_SCRATCH

CYCLES = json.load(open("/tmp/apr_may_backfill.json"))

# (label, expiry_str, picks, is_real) -- last one is REAL, not backtested
CHAIN = [
    ("Mar-Apr", CYCLES["2026-03"]["expiry"], CYCLES["2026-03"]["picks"], False),
    ("Apr-May", CYCLES["2026-04"]["expiry"], CYCLES["2026-04"]["picks"], False),
    ("May-Jun", CYCLES["2026-05"]["expiry"], CYCLES["2026-05"]["picks"], False),
    ("Jun-Jul", CYCLES["2026-06"]["expiry"], CYCLES["2026-06"]["picks"], False),
    ("Jul-Aug", "2026-07-28",
     ["TRENT", "KAYNES", "BANDHANBNK", "GVT&D", "POWERINDIA", "FORCEMOT",
      "SAIL", "IDEA", "AMBER", "ADANIGREEN"], True),
]

TARGETS = {"TRENT", "BANDHANBNK", "POWERINDIA", "GVT&D"}


def build_rows(expiry: dt.date, picks: list) -> list:
    uni = strategy.load_fo_universe()
    hist = strategy.load_price_history(expiry, uni)
    day = sorted(hist)[-1]
    frame = hist[day]
    rows = []
    for sym in picks:
        if sym not in frame.index:
            print(f"  ! {sym} missing from {expiry} bhavcopy -- skipped")
            continue
        close = float(frame.at[sym, "close_price"])
        lo, hi, band_pct = daily_report._compute_stock_entry_band(sym, hist, close)
        rows.append({"symbol": sym, "close": close, "entry_lo": lo, "entry_hi": hi})
    return rows


def main():
    prev_basket = set()
    for label, expiry_str, picks, is_real in CHAIN:
        expiry = dt.date.fromisoformat(expiry_str)
        print(f"\n=== {label} (expiry {expiry}) {'[REAL basket]' if is_real else '[backtested, no ASM veto]'} ===")
        rows = build_rows(expiry, picks)
        this_basket = {r["symbol"] for r in rows}
        to_hold = sorted(this_basket & prev_basket)
        sold = sorted(prev_basket - this_basket)
        if sold:
            print(f"  sold/dropped: {sold}")
            for sym in sold:
                book.close_position(sym, path=BOOK_SCRATCH)

        base_sizing = daily_report._compute_min_portfolio_sizing(rows)
        base_slot = base_sizing["slot_target"]
        base_shares = {s: d["shares"] for s, d in base_sizing["shares"].items()}

        if to_hold:
            ratios = []
            for sym in to_hold:
                pos = book.get(sym, path=BOOK_SCRATCH)
                if pos and pos.get("shares") and base_shares.get(sym):
                    ratios.append(pos["shares"] / base_shares[sym])
            k = max(ratios) if ratios else 1.0
        else:
            k = 1.0
        slot_target = base_slot * k
        floor = max((r["entry_lo"] for r in rows if r.get("entry_lo")), default=0)
        slot_target = max(slot_target, floor)

        sizing = daily_report._resolve_shares_to_target(rows, slot_target)
        rebalance = daily_report._compute_hold_rebalance(rows, to_hold, sizing["slot_target"])

        print(f"  slot_target: {slot_target:,.2f}  (k={k:.3f})")
        for r in rows:
            sym = r["symbol"]
            if sym in to_hold:
                rb = rebalance.get(sym, {})
                status = rb.get("status")
                if status == "ok":
                    shares = rb["shares"]
                elif status == "rebalance":
                    shares = rb["new_shares"]
                else:
                    # no_book_record/no_price_data -- shouldn't happen given
                    # the prior cycle just wrote a record, but guard anyway.
                    shares = sizing["shares"][sym]["shares"]
                note = status or "?"
            else:
                shares = sizing["shares"][sym]["shares"]
                note = "fresh buy"
            flag = "  <== TARGET" if sym in TARGETS else ""
            print(f"    {sym}: {shares} shares ({note}){flag}")
            book.open_position(sym, shares, r["close"], expiry, expiry, path=BOOK_SCRATCH)

        prev_basket = this_basket

    print("\n=== FINAL (as of 28-Jul-2026 cycle open) ===")
    final = book.load(BOOK_SCRATCH)
    for sym in sorted(TARGETS):
        print(f"  {sym}: {final.get(sym)}")


if __name__ == "__main__":
    main()
