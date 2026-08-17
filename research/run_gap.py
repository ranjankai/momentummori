import harness

months = [(2026, 4), (2026, 5), (2026, 6)]   # Apr (cross-check), May, Jun
rows = harness.walk_forward(months)
for r in rows:
    print(f"{r['month']}  breadth {r['breadth']:.1f}%  stop {r['stop']:.0f}%  "
          f"return {r['ret']:+.2f}%  trades {r['trades']}  picks={r['picks']}")
print("sum:", sum(r['ret'] for r in rows))
