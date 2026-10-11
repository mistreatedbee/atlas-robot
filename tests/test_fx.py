from __future__ import annotations

import json
import os
from datetime import date

import pytest

from atlas.fx import FxError, FxSeries, available_at_ms, fetch_usdzar, store_rates
from atlas.http import ReadOnlyHttpClient, Response

from .conftest import utc_ms


def ecb_payload(values: dict[str, float]):
    dates = sorted(values)
    return {"dataSets": [{"series": {"0:0:0:0:0": {"observations": {str(i): [values[d]] for i, d in enumerate(dates)}}}}],
            "structure": {"dimensions": {"observation": [{"values": [{"id": d} for d in dates]}]}}}


def client(payloads):
    queue = list(payloads)
    return ReadOnlyHttpClient({"data-api.ecb.europa.eu"}, min_interval_s=0,
                              transport=lambda url, t: Response(200, {}, json.dumps(queue.pop(0)).encode()))


def test_usdzar_is_zar_per_eur_over_usd_per_eur_on_common_dates():
    http = client([ecb_payload({"2026-10-08": 18.6252, "2026-10-09": 18.527}),
                   ecb_payload({"2026-10-09": 1.1206})])
    (row,) = fetch_usdzar(http, date(2026, 10, 8), date(2026, 10, 9))
    assert row[0] == "2026-10-09" and row[1] == pytest.approx(18.527 / 1.1206)


def test_point_in_time_and_staleness(store):
    store_rates(store, "USDZAR", [("2026-10-08", 16.6), ("2026-10-09", 16.5)], now_ms=0)
    fx = FxSeries.load(store)
    # available from 17:00 Berlin (15:00 UTC in October, CEST)
    assert available_at_ms(date(2026, 10, 9)) == utc_ms(2026, 10, 9, 15)
    assert fx.rate_at(utc_ms(2026, 10, 9, 14, 59)) == 16.6   # 9 Oct rate not yet published
    assert fx.rate_at(utc_ms(2026, 10, 9, 15, 0)) == 16.5
    with pytest.raises(FxError, match="no USDZAR rate"):
        fx.rate_at(utc_ms(2026, 10, 8, 12))
    with pytest.raises(FxError, match="stale"):
        fx.rate_at(utc_ms(2026, 10, 20))
    assert store_rates(store, "USDZAR", [("2026-10-09", 99.0)], now_ms=1) == 0  # never overwritten


def test_empty_store_fails_closed(store):
    with pytest.raises(FxError, match="fx-update"):
        FxSeries.load(store)


def test_only_ecb_host_reachable():
    from atlas.http import HttpError
    http = ReadOnlyHttpClient({"data-api.ecb.europa.eu"}, min_interval_s=0)
    with pytest.raises(HttpError, match="allowlist"):
        http.get_json("https://example.com/x")


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("ATLAS_LIVE_TESTS") != "1", reason="set ATLAS_LIVE_TESTS=1 to run")
def test_live_ecb_usdzar():
    http = ReadOnlyHttpClient({"data-api.ecb.europa.eu"}, min_interval_s=0.5)
    rows = fetch_usdzar(http, date(2026, 10, 5), date(2026, 10, 9))
    assert len(rows) == 5 and all(10 < r < 30 for _, r in rows)
