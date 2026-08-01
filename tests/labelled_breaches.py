"""
The 20 real ratio breaches found in data/cache (388 trading days,
02-Jan-2025 .. 31-Jul-2026), with the theoretical adjustment ratio implied
by the corporate action NSE actually filed.

This is the acceptance set for corporate_actions.classify(). It is real
data, not synthetic: every row was produced by scanning the cached
bhavcopy for day-on-day ratios outside config.V4_SPLIT_RATIO_LOW/HIGH.

`expected` is the theoretically correct multiplier for PRIOR closes.
None means "no correct ratio is derivable from the filing" -- the honest
answer is UNKNOWN / no adjustment. Both such cases are demergers whose
filed subject line is the single word "Demerger".
"""

# (date, symbol, prev_close, close, expected_ratio, note)
LABELLED = [
    ("2025-01-10", "SHRIRAMFIN",  2809.85,  532.00, 0.2000, "split 10->2"),
    ("2025-04-07", "SIEMENS",     4928.15, 2812.45, None,   "demerger, no ratio in filing"),
    ("2025-05-07", "NAUKRI",      6984.50, 1385.00, 0.2000, "split 10->2"),
    ("2025-05-23", "BSE",         6996.50, 2448.00, 0.3333, "bonus 2:1 (dividend also in window)"),
    ("2025-06-04", "COFORGE",     8499.00, 1724.50, 0.2000, "split 10->2"),
    ("2025-06-16", "BAJFINANCE",  9419.50,  938.00, 0.1000, "split 2->1 AND bonus 4:1 same day"),
    ("2025-07-16", "ASHOKLEY",     250.90,  124.60, 0.5000, "bonus 1:1"),
    ("2025-08-08", "NESTLEIND",   2234.60, 1096.50, 0.5000, "bonus 1:1"),
    ("2025-08-26", "HDFCBANK",    1964.10,  973.40, 0.5000, "bonus 1:1"),
    ("2025-09-11", "PATANJALI",   1802.00,  598.90, 0.3333, "bonus 2:1 (dividend also in window)"),
    ("2025-09-16", "GODFRYPHLP", 10229.00, 3644.00, 0.3333, "bonus 2:1"),
    ("2025-09-22", "ADANIPOWER",   709.40,  170.25, 0.2000, "split 10->2"),
    ("2025-09-23", "PIDILITIND",  3038.00, 1489.30, 0.5000, "bonus 1:1"),
    ("2025-11-26", "HDFCAMC",     5336.50, 2679.00, 0.5000, "bonus 1:1"),
    ("2025-12-05", "CAMS",        3956.70,  775.70, 0.2000, "split 10->2"),
    ("2026-01-02", "MCX",        10989.00, 2216.00, 0.2000, "split 10->2"),
    ("2026-01-14", "KOTAKBANK",   2132.60,  421.00, 0.2000, "split 5->1"),
    ("2026-02-26", "ANGELONE",    2489.90,  246.50, 0.1000, "split 10->1"),
    ("2026-04-30", "VEDL",         773.60,  271.55, None,   "demerger, no ratio in filing"),
    ("2026-05-29", "LICI",         830.00,  411.35, 0.5000, "bonus 1:1"),
]
