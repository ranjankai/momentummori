"""
Variant of book_backfill.py: what if the investor started fresh at the
June-2026 expiry (Jun-Jul cycle) instead of March-2026? Only two legs
(Jun-Jul Day-0, then Jul-Aug top-up into the REAL known basket) instead
of the full four-cycle chain -- and critically, Day-0 sizing here is NOT
inflated by any earlier coverage-scale carry-over, since this investor
has no holdings before June. Same production functions, same no-ASM-
veto caveat on the June basket as the other script.
"""
import datetime as dt
import json
import sys

sys.path.insert(0, "..")
import strategy                                              # noqa: E402
import daily_report                                           # noqa: E402
import book                                                    # noqa: E402

BOOK_SCRATCH = "/tmp/book_backfill_junstart.json"
book.BOOK_FILE = BOOK_SCRATCH

CYCLES = json.load(open("/tmp/apr_may_backfill.json"))

CHAIN = [
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
                    shares = sizing["shares"][sym]["shares"]
                note = status or "?"
            else:
                shares = sizing["shares"][sym]["shares"]
                note = "fresh buy"
            flag = "  <== TARGET" if sym in TARGETS else ""
            print(f"    {sym}: {shares} shares ({note}){flag}")
            book.open_position(sym, shares, r["close"], expiry, expiry, path=BOOK_SCRATCH)

        prev_basket = this_basket

    print("\n=== FINAL (June-start investor, as of 28-Jul-2026 cycle open) ===")
    final = book.load(BOOK_SCRATCH)
    for sym in sorted(TARGETS):
        print(f"  {sym}: {final.get(sym)}")


if __name__ == "__main__":
    main()
