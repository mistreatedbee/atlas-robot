from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from atlas.http import HttpError, ReadOnlyHttpClient, Response
from atlas.models import DataLabel, PriceBasis, VolumeKind
from atlas.providers import BinanceSpotProvider, CsvFileProvider
from atlas.providers.csv_file import parse_time_ms

from .conftest import H1, utc_ms

FIXTURE = Path(__file__).parent / "fixtures" / "binance_btcusdt_1h_recorded.json"
HOST = "data-api.binance.vision"


class ScriptedTransport:
    """Returns queued responses (or raises queued exceptions) and records requested URLs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body, headers=None):
    return Response(200, headers or {}, json.dumps(body).encode() if not isinstance(body, bytes) else body)


def client(transport, sleeps=None):
    return ReadOnlyHttpClient({HOST}, min_interval_s=0, backoff_base_s=1, transport=transport,
                              sleep=(sleeps.append if sleeps is not None else lambda s: None))


# ---- HTTP client -----------------------------------------------------------------------------

def test_http_refuses_hosts_outside_allowlist():
    with pytest.raises(HttpError, match="allowlist"):
        client(ScriptedTransport()).get_json("https://api.binance.com/api/v3/klines")


def test_http_refuses_plain_http():
    with pytest.raises(HttpError, match="non-HTTPS"):
        client(ScriptedTransport()).get_json(f"http://{HOST}/api/v3/klines")


def test_http_retries_429_honouring_retry_after():
    sleeps = []
    t = ScriptedTransport(Response(429, {"retry-after": "7"}, b""), ok([1]))
    data, _ = client(t, sleeps).get_json(f"https://{HOST}/x")
    assert data == [1] and sleeps == [7.0] and len(t.urls) == 2


def test_http_retries_5xx_and_network_errors_with_backoff():
    sleeps = []
    t = ScriptedTransport(Response(503, {}, b""), ConnectionError("reset"), ok([2]))
    data, _ = client(t, sleeps).get_json(f"https://{HOST}/x")
    assert data == [2] and sleeps == [1, 2]


def test_http_gives_up_after_max_retries():
    t = ScriptedTransport(*[Response(500, {}, b"")] * 6)
    with pytest.raises(HttpError, match="giving up"):
        client(t).get_json(f"https://{HOST}/x")


def test_http_fails_fast_on_certificate_errors():
    import ssl
    import urllib.error
    t = ScriptedTransport(urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer")))
    with pytest.raises(HttpError, match="certificate"):
        client(t).get_json(f"https://{HOST}/x")
    assert len(t.urls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_http_does_not_retry_client_errors_or_bans(status):
    t = ScriptedTransport(Response(status, {}, b"{}"))
    with pytest.raises(HttpError):
        client(t).get_json(f"https://{HOST}/x")
    assert len(t.urls) == 1


# ---- Binance ---------------------------------------------------------------------------------

def test_binance_parses_recorded_response():
    rows = json.loads(FIXTURE.read_text())
    t = ScriptedTransport(ok(rows, {"x-mbx-used-weight-1m": "2"}))
    provider = BinanceSpotProvider(client(t))
    start = utc_ms(2026, 10, 11)
    result = provider.fetch("BTCUSDT", "BTCUSDT", "H1", start, start + 3 * H1)

    q = parse_qs(urlsplit(t.urls[0]).query)
    assert q["interval"] == ["1h"] and q["startTime"] == [str(start)]
    assert q["endTime"] == [str(start + 3 * H1 - 1)] and q["limit"] == ["1000"]

    assert result.rejects == [] and len(result.candles) == len(rows) == 3
    c = result.candles[0]
    assert (c.open_time, c.close_time) == (rows[0][0], rows[0][6] + 1)
    assert (c.open, c.high, c.low, c.close) == tuple(float(v) for v in rows[0][1:5])
    assert c.volume == float(rows[0][5]) and c.quote_volume == float(rows[0][7]) and c.trade_count == rows[0][8]
    assert c.volume_kind is VolumeKind.BASE_ASSET_TRADED and c.price_basis is PriceBasis.TRADE
    assert provider.data_label is DataLabel.LIVE_SOURCE


def test_binance_rejects_rows_with_inconsistent_close_time_or_garbage():
    rows = json.loads(FIXTURE.read_text())
    bad_close = list(rows[0]); bad_close[6] = bad_close[0] + 59_999  # a 1m bar in an H1 request
    garbage = ["x"]
    t = ScriptedTransport(ok([bad_close, garbage, rows[1]]))
    result = BinanceSpotProvider(client(t)).fetch("BTCUSDT", "BTCUSDT", "H1", rows[0][0], rows[0][0] + 3 * H1)
    assert len(result.candles) == 1 and len(result.rejects) == 2


def test_binance_pauses_when_weight_budget_reached():
    sleeps = []
    t = ScriptedTransport(ok([], {"x-mbx-used-weight-1m": "3500"}))
    provider = BinanceSpotProvider(client(t), weight_safety_fraction=0.5, sleep=sleeps.append,
                                   wall_clock=lambda: 1_000_040.0)
    provider.fetch("BTCUSDT", "BTCUSDT", "H1", 0, H1)
    assert sleeps == [41.0]  # until just after the next minute boundary


# ---- CSV -------------------------------------------------------------------------------------

def test_csv_naive_times_use_declared_source_timezone_across_dst():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Athens")  # typical MT5 server-time convention (UTC+2 winter / UTC+3 summer)
    assert parse_time_ms("2026-01-15 02:00", tz) == utc_ms(2026, 1, 15, 0)
    assert parse_time_ms("2026-07-15 03:00", tz) == utc_ms(2026, 7, 15, 0)
    assert parse_time_ms("2026-07-15T03:00:00Z", tz) == utc_ms(2026, 7, 15, 3)  # explicit offset wins


def test_csv_provider_parses_and_rejects(tmp_path):
    f = tmp_path / "EURUSD_H1.csv"
    f.write_text(
        "time,open,high,low,close,volume\n"
        "2026-10-07T00:00:00Z,1.1,1.2,1.0,1.15,10\n"
        "2026-10-07T01:00:00Z,1.15,oops,1.0,1.1,10\n"
        "2026-10-07T02:00:00Z,1.1,1.2,1.0,1.15,\n"
    )
    p = CsvFileProvider(name="user_csv", venue="v", path_template=tmp_path / "{symbol}_{timeframe}.csv",
                        data_label=DataLabel.USER_FILE, price_basis=PriceBasis.BID, volume_kind=VolumeKind.TICK_COUNT)
    res = p.fetch("EURUSD", "EURUSD", "H1", utc_ms(2026, 10, 7), utc_ms(2026, 10, 8))
    assert len(res.candles) == 2 and len(res.rejects) == 1 and "line 3" in res.rejects[0].raw
    assert res.candles[0].bid_close == 1.15  # bid basis fills bid columns
    assert res.candles[1].volume is None and res.candles[1].volume_kind is VolumeKind.NONE
    again = p.fetch("EURUSD", "EURUSD", "H1", utc_ms(2026, 10, 7), utc_ms(2026, 10, 8))
    assert again.rejects == []  # reported once per provider instance


def test_csv_can_never_claim_to_be_live(tmp_path):
    with pytest.raises(ValueError):
        CsvFileProvider(name="x", venue="v", path_template=tmp_path / "x.csv", data_label=DataLabel.LIVE_SOURCE,
                        price_basis=PriceBasis.BID, volume_kind=VolumeKind.NONE)
