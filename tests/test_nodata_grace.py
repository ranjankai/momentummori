"""
A 404 for a recent date means "not published yet", not "never".

On 04-Aug-2026 two catch-up runs at 01:05 and 01:07 asked NSE for that
day's bhavcopy. It publishes around 18:00 IST, so both got a 404 and the
client wrote a permanent no-data marker. The file appeared at 18:00 and
was never re-fetched: every run that evening reported "2026-08-04 is not
a trading day" and no note was sent.
"""
import datetime as dt
import os

import pytest

import nse_client


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(nse_client.config, "CACHE_DIR", str(tmp_path))
    return tmp_path


def test_no_marker_is_written_for_a_date_that_may_still_publish():
    today = dt.date.today()
    nse_client._mark_nodata("cm", today)
    assert not os.path.exists(nse_client._nodata_path("cm", today)), \
        "a 404 for today was recorded as permanent"


def test_marker_is_written_for_an_old_date():
    """Holidays must still be cached, or every backtest re-requests them."""
    old = dt.date.today() - dt.timedelta(days=60)
    nse_client._mark_nodata("cm", old)
    assert os.path.exists(nse_client._nodata_path("cm", old))


def test_grace_boundary():
    g = nse_client.NODATA_GRACE_DAYS
    today = dt.date.today()
    assert not nse_client._nodata_is_authoritative(today - dt.timedelta(days=g))
    assert nse_client._nodata_is_authoritative(today - dt.timedelta(days=g + 1))


def test_a_stale_marker_is_discarded_and_the_fetch_retried(monkeypatch):
    """
    The exact 04-Aug-2026 failure: a marker exists for a recent date, so
    the client must delete it and ask NSE again rather than short-circuit.
    """
    today = dt.date.today()
    path = nse_client._nodata_path("cm", today)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("stale\n")

    calls = []

    def fake_download(url):
        calls.append(url)
        raise nse_client.NseNoDataError("still not out")

    monkeypatch.setattr(nse_client, "_download_with_retry", fake_download)
    with pytest.raises(nse_client.NseNoDataError):
        nse_client._fetch_bhavcopy("cm", "http://x/{date:%Y%m%d}", set(), today, True)

    assert calls, "the stale marker short-circuited the fetch"
    assert not os.path.exists(path), "the stale marker was not removed"


def test_an_old_marker_still_short_circuits(monkeypatch):
    old = dt.date.today() - dt.timedelta(days=60)
    with open(nse_client._nodata_path("cm", old), "w", encoding="utf-8") as fh:
        fh.write("holiday\n")

    calls = []
    monkeypatch.setattr(nse_client, "_download_with_retry",
                        lambda url: calls.append(url))
    with pytest.raises(nse_client.NseNoDataError):
        nse_client._fetch_bhavcopy("cm", "http://x/{date:%Y%m%d}", set(), old, True)
    assert not calls, "an old holiday marker should avoid the network"
