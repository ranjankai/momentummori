import json
import glob

files = sorted(glob.glob("data/picks_*.json"))
for fn in files:
    data = json.load(open(fn))
    m = data["month"]
    reg = data["regime"]
    picks = data["picks"]
    print(f"=== AUDITING {fn} ({m}, {reg}) ===")
    assert len(picks) == 10, f"Expected 10 picks, got {len(picks)}"
    ranks = [p["rank"] for p in picks]
    assert ranks == list(range(1, 11)), f"Invalid ranks: {ranks}"

    sec_counts = {}
    for p in picks:
        s = p["sector"]
        sec_counts[s] = sec_counts.get(s, 0) + 1
        assert p["from52wh"] <= 0.0001, f"Invalid positive from52wh: {p['symbol']}={p['from52wh']}"
        assert p["turnover_cr"] >= 5.0, f"Violated turnover floor: {p['symbol']}={p['turnover_cr']}"

    max_sec = max(sec_counts.values())
    assert max_sec <= 3, f"Sector cap exceeded: {sec_counts}"
    
    print(f"  Picks count: {len(picks)} (1 to 10 verified)")
    print(f"  Max sector count: {max_sec} ({sec_counts})")
    print(f"  from52wh range: [{min(p['from52wh'] for p in picks):.4f}, {max(p['from52wh'] for p in picks):.4f}]")
    print("  AUDIT PASSED CLEANLY!\n")
