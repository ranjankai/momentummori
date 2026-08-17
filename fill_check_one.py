import sys, time
sys.path.insert(0, ".")
sys.path.insert(0, "research")
import logging
logging.disable(logging.CRITICAL)
import daily_report, harness, strategy

t0=time.time()
ex, nx = harness.cycle_dates(2025, 3)
print("cycle_dates", ex, nx, time.time()-t0)
picks, ranked, stop_pct, breadth = harness.v4_basket(ex, top_n=10)
print("v4_basket done", time.time()-t0, picks)
