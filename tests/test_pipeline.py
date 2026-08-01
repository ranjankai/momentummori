"""
End-to-end pipeline test using a monkeypatched nse_client (no network).
This is the closest we can get to a real run inside a sandboxed
environment that cannot reach nseindia.com — it proves the plumbing
(fetch -> normalize -> score -> rank -> cache -> JSON) works together
correctly. It does NOT prove the live NSE URLs/schema are still
current; see nse_client.py's module docstring.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

import config
import nse_client
import pipeline


SYMBOLS = ["AAA", "BBB", "CCC"]


def _synthetic_cm_bhavcopy(trade_date, trend):
    """trend: dict symbol -> close price for this date."""
    return pd.DataFrame(
        {
            "TckrSymb": SYMBOLS,
            "ClsPric": [trend[s] for s in SYMBOLS],
            "TtlTradgVol": [1_000_000, 500_000, 250_000],
            "TradDt": [str(trade_date)] * 3,
        }
    )


def _synthetic_fo_bhavcopy(trade_date, expiry_near, expiry_next):
    rows = []
    oi_near = {"AAA": 1000, "BBB": 4000, "CCC": 2500}
    oi_next = {"AAA": 4000, "BBB": 1000, "CCC": 2500}
    settle = {"AAA": 210, "BBB": 90, "CCC": 150}
    for s in SYMBOLS:
        rows.append(
            {
                "TckrSymb": s, "FinInstrmTp": "FUTSTK", "XpryDt": str(expiry_near),
                "OpnIntrst": oi_near[s], "SttlmPric": settle[s], "TtlTradgVol": 10000,
            }
        )
        rows.append(
            {
                "TckrSymb": s, "FinInstrmTp": "FUTSTK", "XpryDt": str(expiry_next),
                "OpnIntrst": oi_next[s], "SttlmPric": settle[s] + 1, "TtlTradgVol": 8000,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "data" / "cache"))
    monkeypatch.setattr(config, "LATEST_RESULT_FILE", str(tmp_path / "data" / "latest.json"))
    monkeypatch.setattr(
        config, "UNIVERSE_FILE", str(tmp_path / "universe.csv")
    )
    pd.DataFrame({"symbol": SYMBOLS}).to_csv(config.UNIVERSE_FILE, index=False)
    monkeypatch.setattr(config, "SECTOR_MAP_FILE", str(tmp_path / "sectors.csv"))
    pd.DataFrame({"symbol": SYMBOLS, "sector": ["SectorA", "SectorB", "SectorC"]}).to_csv(
        config.SECTOR_MAP_FILE, index=False
    )
    return tmp_path


def test_load_sector_map_missing_file_degrades_gracefully(isolated_config, monkeypatch):
    """If the sector file is missing, pipeline should still run (no cap), not crash."""
    monkeypatch.setattr(config, "SECTOR_MAP_FILE", str(isolated_config / "does_not_exist.csv"))
    result = pipeline.load_sector_map()
    assert result == {}


def test_pipeline_run_end_to_end(isolated_config, monkeypatch):
    as_of = date(2026, 7, 27)
    expiry_near = date(2026, 7, 30)
    expiry_next = date(2026, 8, 27)

    # AAA trends up steadily, BBB trends down, CCC is flat -> AAA should rank first.
    price_paths = {
        as_of - timedelta(days=offset): {
            "AAA": 100 + (20 - offset) * 2,
            "BBB": 300 - (20 - offset) * 2,
            "CCC": 150,
        }
        for offset in range(0, 25)
    }

    def fake_fetch_cm(trade_date, use_cache=True):
        if trade_date.weekday() >= 5 or trade_date not in price_paths:
            raise nse_client.NseFetchError("no trading data (synthetic)")
        return _synthetic_cm_bhavcopy(trade_date, price_paths[trade_date])

    def fake_fetch_fo(trade_date, use_cache=True):
        return _synthetic_fo_bhavcopy(trade_date, expiry_near, expiry_next)

    monkeypatch.setattr(nse_client, "fetch_cm_bhavcopy", fake_fetch_cm)
    monkeypatch.setattr(nse_client, "fetch_fo_bhavcopy", fake_fetch_fo)

    result = pipeline.run(as_of=as_of)

    assert result["universe_size"] == 3
    assert len(result["rankings"]) > 0
    symbols_in_order = [r["symbol"] for r in result["rankings"]]
    assert symbols_in_order[0] == "AAA"  # rising price + heavy rollover + premium

    cached = pipeline.load_latest_cached()
    assert cached["as_of"] == str(as_of)


def test_pipeline_uses_last_trading_day_for_fo_when_as_of_is_a_holiday(isolated_config, monkeypatch):
    """
    Regression test: as_of (e.g. "day before month start" in a backtest)
    can land on a weekday market holiday with no bhavcopy at all. The FO
    fetch must use the last date we actually got CM data for, not the
    raw as_of, or the whole run used to crash with an uncaught
    NseFetchError (this exact scenario broke a live backtest run: 31
    Mar 2025 was a holiday and killed the whole multi-month script).
    """
    as_of = date(2025, 3, 31)          # simulated holiday: no bhavcopy exists
    last_trading_day = date(2025, 3, 28)  # the Friday before it
    expiry_near = date(2025, 4, 24)
    expiry_next = date(2025, 5, 29)

    price_paths = {
        last_trading_day - timedelta(days=offset): {"AAA": 100 + offset, "BBB": 200 - offset, "CCC": 150}
        for offset in range(0, 20)
    }

    def fake_fetch_cm(trade_date, use_cache=True):
        if trade_date == as_of or trade_date.weekday() >= 5 or trade_date not in price_paths:
            raise nse_client.NseFetchError("holiday/weekend (synthetic)")
        return _synthetic_cm_bhavcopy(trade_date, price_paths[trade_date])

    fo_calls = []

    def fake_fetch_fo(trade_date, use_cache=True):
        fo_calls.append(trade_date)
        if trade_date == as_of:
            raise nse_client.NseFetchError("holiday: no FO bhavcopy (synthetic)")
        return _synthetic_fo_bhavcopy(trade_date, expiry_near, expiry_next)

    monkeypatch.setattr(nse_client, "fetch_cm_bhavcopy", fake_fetch_cm)
    monkeypatch.setattr(nse_client, "fetch_fo_bhavcopy", fake_fetch_fo)

    result = pipeline.run(as_of=as_of, persist_cache=False)

    assert result["latest_bhavcopy_date"] == str(last_trading_day)
    assert as_of not in fo_calls, "FO bhavcopy must not be requested for the holiday date itself"
    assert last_trading_day in fo_calls
    assert len(result["rankings"]) > 0


def test_pipeline_raises_clear_error_when_all_fetches_fail(isolated_config, monkeypatch):
    def always_fail(trade_date, use_cache=True):
        raise nse_client.NseFetchError("NSE unreachable (synthetic failure)")

    monkeypatch.setattr(nse_client, "fetch_cm_bhavcopy", always_fail)
    monkeypatch.setattr(nse_client, "fetch_fo_bhavcopy", always_fail)

    with pytest.raises(RuntimeError, match="No cash-market bhavcopy data"):
        pipeline.run(as_of=date(2026, 7, 27))
