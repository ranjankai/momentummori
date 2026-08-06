import os
import json
import datetime
import pandas as pd
import numpy as np
import logging

logging.disable(logging.CRITICAL)

import strategy
import scoring
import nse_client

# Exact verified ban lists for selection dates
KNOWN_BAN_MAP = {
    datetime.date(2026, 1, 30): set(),
    datetime.date(2026, 2, 27): {"SAMMAANCAP"},
    datetime.date(2026, 3, 30): {"SAIL"},
    datetime.date(2026, 6, 30): set()
}

def z_score(series):
    std = series.std(ddof=0)
    if std == 0 or pd.isna(std):
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std

def get_selection_date(year, month):
    days = sorted([d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d))
                   for d in strategy.known_trading_days()])
    target = datetime.date(year, month, 1)
    before = [d for d in days if d < target]
    return max(before)

def load_cached_price_history(sel_date, num_days=270):
    known = sorted([d for d in strategy.known_trading_days() if d <= sel_date])[-num_days:]
    hist = {}
    for d in known:
        raw = nse_client.fetch_cm_bhavcopy(d)
        norm = scoring.normalize_cm_columns(raw)
        # Fix: symbol-keyed reindex for OHLC columns to avoid positional index mismatch across series-filtered DataFrames
        raw_ohlc = raw.drop_duplicates(subset=['TckrSymb'], keep='first').set_index('TckrSymb')
        for src, dst in [('OpnPric', 'open_price'), ('HghPric', 'high_price'), ('LwPric', 'low_price')]:
            if src in raw_ohlc.columns:
                norm[dst] = pd.to_numeric(raw_ohlc[src].reindex(norm['symbol']).values, errors='coerce')
        hist[d] = norm.drop_duplicates('symbol', keep='first').set_index('symbol')
    return hist

def run_picking_for_month(year, month, uni, sec):
    sel_date = get_selection_date(year, month)
    month_str = f"{year}-{month:02d}"
    print(f"\n=======================================================")
    print(f"  RUNNING PICKING METHOD FOR {month_str} (Selection Date: {sel_date})")
    print(f"=======================================================")

    ban_set = KNOWN_BAN_MAP.get(sel_date, set())
    print(f"F&O Ban list for {sel_date}: {sorted(list(ban_set)) if ban_set else 'None'}")

    # Load cached trading days up to sel_date
    print(f"Loading cached trading days up to {sel_date}...")
    month_hist = load_cached_price_history(sel_date, num_days=270)
    
    # MANDATORY: back_adjust=True to restate history for corporate actions/splits
    print("Applying back-adjustment for corporate actions (heuristic mode)...")
    month_hist = strategy.adjust_holding_window(month_hist, sorted(month_hist), back_adjust=True, use_classifier=False)
    dates = sorted(month_hist.keys())

    # Build feature table per symbol
    feature_rows = []
    for s in uni:
        cl, hi, lo, vol, to = [], [], [], [], []
        for x in dates:
            f = month_hist[x]
            if s not in f.index:
                continue
            c = f.at[s, 'close_price']
            if pd.isna(c) or c <= 0:
                continue
            cl.append(float(c))
            hi.append(float(f.at[s, 'high_price']) if pd.notna(f.at[s, 'high_price']) else float(c))
            lo.append(float(f.at[s, 'low_price']) if pd.notna(f.at[s, 'low_price']) else float(c))
            to.append(float(f.at[s, 'turnover']) if ('turnover' in f.columns and pd.notna(f.at[s, 'turnover'])) else 0.0)

        if len(cl) < 210:
            continue

        c = cl[-1]
        r5 = c / cl[-6] - 1.0 if len(cl) >= 6 else 0.0
        r20 = c / cl[-21] - 1.0
        r60 = c / cl[-61] - 1.0
        dma20 = float(np.mean(cl[-20:]))
        dma50 = float(np.mean(cl[-50:]))
        dma100 = float(np.mean(cl[-100:]))
        dma200 = float(np.mean(cl[-200:]))
        
        stack = int(c > dma20) + int(dma20 > dma50) + int(dma50 > dma100) + int(dma100 > dma200)

        # 52w high over last 252 sessions
        hi252 = max(hi[-252:]) if len(hi) >= 252 else max(hi)
        from52wh = (c / hi252) - 1.0

        # HV20: stdev of daily returns over last 20 sessions * sqrt(252) * 100
        rets20 = pd.Series(cl[-21:]).pct_change().dropna()
        hv20 = float(rets20.std(ddof=1) * np.sqrt(252) * 100) if len(rets20) >= 20 else 0.0

        # ATR20
        tr_list = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
                   for i in range(len(cl) - 20, len(cl))]
        atr20 = float(np.mean(tr_list))

        # Turnover last 20 sessions mean in Crore (1 Cr = 1e7 INR)
        to20 = np.mean(to[-20:]) if len(to) >= 20 else 0.0
        turnover_cr = float(to20 / 1e7)

        # Volume surge: mean turnover last 5 / mean turnover last 20
        to5 = np.mean(to[-5:]) if len(to) >= 5 else 0.0
        volsurge = float(to5 / to20) if to20 > 0 else 1.0

        s_sector = sec.get(s, "OTHERS")

        feature_rows.append({
            'symbol': s,
            'sector': s_sector,
            'close': c,
            'r5': r5,
            'r20': r20,
            'r60': r60,
            'dma20': dma20,
            'dma50': dma50,
            'dma100': dma100,
            'dma200': dma200,
            'stack': stack,
            'from52wh': from52wh,
            'hv20': hv20,
            'atr20': atr20,
            'volsurge': volsurge,
            'turnover_cr': turnover_cr,
            'sessions': len(cl)
        })

    df = pd.DataFrame(feature_rows)
    print(f"Eligible universe count (>=210 sessions): {len(df)}")

    # 2. Read the Regime
    breadth = float((df['close'] > df['dma200']).mean() * 100)
    med_r20 = float(df['r20'].median() * 100)
    med_r60 = float(df['r60'].median() * 100)
    pct_above_20_50 = float(((df['close'] > df['dma20']) & (df['close'] > df['dma50'])).mean() * 100)

    if med_r20 > 0 and pct_above_20_50 > 35.0:
        regime = "TRENDING"
    elif med_r20 < -5.0 or pct_above_20_50 < 15.0:
        regime = "DAMAGED"
    else:
        regime = "MIXED"

    print(f"Regime: {regime}")
    print(f"  breadth: {breadth:.2f}% (above 200 DMA)")
    print(f"  med_r20: {med_r20:.2f}%")
    print(f"  med_r60: {med_r60:.2f}%")
    print(f"  pct_above_20_50: {pct_above_20_50:.2f}%")

    # Sector Rank / Median R20
    sector_medians = df.groupby('sector')['r20'].median().to_dict()
    df['sector_med_r20'] = df['sector'].map(sector_medians)
    # Sector leaders (top 3 sectors by median r20)
    top_sectors = sorted(sector_medians.items(), key=lambda x: x[1], reverse=True)[:3]
    top_sector_names = set(s[0] for s in top_sectors)
    print(f"Top 3 Sectors: {[f'{s}:{m*100:.1f}%' for s,m in top_sectors]}")

    # Sector rank z score
    z_sector = z_score(df['sector_med_r20'])
    z_r20 = z_score(df['r20'])
    z_r60 = z_score(df['r60'])
    z_from52wh = z_score(df['from52wh'])
    z_neg_from52wh = z_score(-df['from52wh'])
    z_rel_r20 = z_score(df['r20'] - (med_r20 / 100.0))
    z_volsurge = z_score(df['volsurge'])

    # 4. Compute composite score based on regime
    if regime == "TRENDING":
        df['score'] = 0.35 * z_r20 + 0.20 * z_r60 + 0.20 * z_from52wh + 0.15 * z_sector + 0.10 * z_volsurge
    elif regime == "DAMAGED":
        df['score'] = 0.40 * z_neg_from52wh + 0.30 * z_rel_r20 + 0.20 * z_sector + 0.10 * z_volsurge
    else:  # MIXED
        df['score'] = 0.40 * z_r20 + 0.30 * z_r60 + 0.20 * z_sector + 0.10 * z_volsurge

    # 3. Hard Filters
    df['pass_liquidity'] = df['turnover_cr'] >= 5.0
    df['pass_ban'] = ~df['symbol'].isin(ban_set)
    df['pass_dma'] = True
    if regime == "TRENDING":
        df['pass_dma'] = (df['close'] > df['dma20']) & (df['close'] > df['dma50'])
    elif regime == "MIXED":
        df['pass_dma'] = df['close'] > df['dma20']

    # 6. Judgement Overlay 1: Reject dead-cat bounce in TRENDING/MIXED
    # Name > 40% below 52w high (from52wh < -0.40) with hv20 > 50
    df['is_dead_cat'] = (df['from52wh'] < -0.40) & (df['hv20'] > 50.0)

    overrides_applied = []
    filters_relaxed = []

    # Filter base candidate pool
    pool = df[df['pass_liquidity'] & df['pass_ban']].copy()

    # Exclude dead cat in TRENDING/MIXED
    if regime in ["TRENDING", "MIXED"]:
        dead_cats = pool[pool['pass_dma'] & pool['is_dead_cat']]
        for _, dc in dead_cats.iterrows():
            overrides_applied.append(
                f"Rejected dead-cat candidate {dc['symbol']} (r20={dc['r20']*100:.1f}%, from52wh={dc['from52wh']*100:.1f}%, hv20={dc['hv20']:.1f})"
            )
        pool = pool[~((pool['from52wh'] < -0.40) & (pool['hv20'] > 50.0))]

    # Candidate selection algorithm
    strict_pool = pool[pool['pass_dma']].sort_values('score', ascending=False)

    selected = []
    sector_counts = {}

    def try_add(row, max_sec_cap):
        sec_name = row['sector']
        cur_cnt = sector_counts.get(sec_name, 0)
        allowed_cap = max_sec_cap
        if cur_cnt == 2 and sec_name not in top_sector_names and max_sec_cap >= 3:
            allowed_cap = 2

        if cur_cnt < allowed_cap:
            sector_counts[sec_name] = cur_cnt + 1
            selected.append(row)
            return True
        return False

    # Round 1: strict DMA, cap=3
    for _, row in strict_pool.iterrows():
        if len(selected) == 10:
            break
        try_add(row, max_sec_cap=3)

    # If short, Step 5 relaxation steps:
    if len(selected) < 10:
        filters_relaxed.append("Relaxed regime DMA filter for remaining slots")
        print(f"Strict filter gave {len(selected)} picks. Relaxing DMA filter...")
        remaining_pool = pool[~pool['symbol'].isin([r['symbol'] for r in selected])].sort_values('score', ascending=False)
        for _, row in remaining_pool.iterrows():
            if len(selected) == 10:
                break
            try_add(row, max_sec_cap=3)

    if len(selected) < 10:
        filters_relaxed.append("Relaxed sector cap from 3 to 4")
        print(f"Pool gave {len(selected)} picks with sector cap 3. Relaxing sector cap to 4...")
        remaining_pool = pool[~pool['symbol'].isin([r['symbol'] for r in selected])].sort_values('score', ascending=False)
        for _, row in remaining_pool.iterrows():
            if len(selected) == 10:
                break
            try_add(row, max_sec_cap=4)

    if len(selected) < 10:
        filters_relaxed.append("Filled remaining slots from highest-ranked liquid names regardless of trend/sector cap")
        remaining_pool = pool[~pool['symbol'].isin([r['symbol'] for r in selected])].sort_values('score', ascending=False)
        for _, row in remaining_pool.iterrows():
            if len(selected) == 10:
                break
            sec_name = row['sector']
            sector_counts[sec_name] = sector_counts.get(sec_name, 0) + 1
            selected.append(row)

    # 6. Judgement Overlay 2: Prefer intact chart when two names score within ~10%
    for i in range(len(selected) - 1):
        r1, r2 = selected[i], selected[i+1]
        score_diff_pct = abs(r1['score'] - r2['score']) / abs(r1['score']) if r1['score'] != 0 else 0
        if score_diff_pct <= 0.10:
            if r2['from52wh'] > r1['from52wh'] + 0.05:
                overrides_applied.append(
                    f"Swapped rank {i+1} ({r1['symbol']}, from52wh={r1['from52wh']*100:.1f}%) and rank {i+2} ({r2['symbol']}, from52wh={r2['from52wh']*100:.1f}%) due to chart proximity to 52w high (scores within 10%)"
                )
                selected[i], selected[i+1] = selected[i+1], selected[i]

    print(f"\nFinal Selected 10 picks for {month_str}:")
    picks_output = []
    for rank, row in enumerate(selected, 1):
        reason_parts = [
            f"r20={row['r20']*100:+.1f}%",
            f"r60={row['r60']*100:+.1f}%",
            f"from52wh={row['from52wh']*100:+.1f}%",
            f"hv20={row['hv20']:.1f}",
            f"turnover={row['turnover_cr']:.1f}cr"
        ]
        reason = ", ".join(reason_parts)
        print(f" {rank:2d}. {row['symbol']:12s} | {row['sector']:15s} | {reason}")
        picks_output.append({
            "rank": rank,
            "symbol": row['symbol'],
            "sector": row['sector'],
            "r20": round(float(row['r20']), 4),
            "r60": round(float(row['r60']), 4),
            "from52wh": round(float(row['from52wh']), 4),
            "hv20": round(float(row['hv20']), 2),
            "turnover_cr": round(float(row['turnover_cr']), 2),
            "score": round(float(row['score']), 4),
            "reason": reason
        })

    out_data = {
        "month": month_str,
        "selection_date": sel_date.isoformat(),
        "regime": regime,
        "breadth": round(breadth, 2),
        "med_r20": round(med_r20, 2),
        "med_r60": round(med_r60, 2),
        "pct_above_20_50": round(pct_above_20_50, 2),
        "picks": picks_output,
        "overrides_applied": overrides_applied,
        "filters_relaxed": filters_relaxed if filters_relaxed else "none"
    }

    out_path = os.path.join("data", f"picks_{month_str}.json")
    with open(out_path, "w") as f:
        json.dump(out_data, f, indent=2)
    print(f"Saved picks to {out_path}")
    return out_data

if __name__ == "__main__":
    uni = strategy.load_fo_universe()
    sec = strategy.load_sector_map()
    months = [(2026, 2), (2026, 3), (2026, 4), (2026, 7)]

    results = {}
    for y, m in months:
        results[f"{y}-{m:02d}"] = run_picking_for_month(y, m, uni, sec)
    print("\nALL 4 MONTHS COMPLETED SUCCESSFULLY!")
