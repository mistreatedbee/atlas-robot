from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.config import Instrument
from atlas.models import Candle, DataLabel, FetchResult, PriceBasis, RejectedRecord, VolumeKind, timeframe_ms
from atlas.storage import Store

ROOT = Path(__file__).resolve().parents[1]
H1 = timeframe_ms("H1")


def utc_ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def make_candle(open_time: int, *, tf: str = "H1", price: float = 100.0, **overrides) -> Candle:
    base = Candle(
        instrument_id="BTCUSDT", provider="fake", venue="fakevenue", timeframe=tf, source_symbol="BTCUSDT",
        open_time=open_time, close_time=open_time + timeframe_ms(tf),
        open=price, high=price + 2, low=price - 2, close=price + 1,
        price_basis=PriceBasis.TRADE, volume=10.0, volume_kind=VolumeKind.BASE_ASSET_TRADED,
    )
    return replace(base, **overrides)


CRYPTO = Instrument("BTCUSDT", "crypto_spot", "BTC", "USDT", "crypto_24x7", 0.01, {"fake": "BTCUSDT"})
FX = Instrument("EURUSD", "fx_spot", "EUR", "USD", "fx_24x5", 0.00001, {"fake": "EURUSD"})


class FakeProvider:
    """In-memory provider. Generates one candle per bar; can fail on the Nth fetch call."""

    name = "fake"
    venue = "fakevenue"
    data_label = DataLabel.LIVE_SOURCE
    timeframes = frozenset({"H1", "M15"})

    def __init__(self, *, max_bars: int = 24, fail_on_call: int | None = None, missing: set[int] = frozenset(),
                 extra_unfinished: bool = False, rejects_per_call: int = 0, price_shift: float = 0.0):
        self.max_bars_per_request = max_bars
        self.fail_on_call = fail_on_call
        self.missing = set(missing)
        self.extra_unfinished = extra_unfinished
        self.rejects_per_call = rejects_per_call
        self.price_shift = price_shift
        self.calls: list[tuple[int, int]] = []

    def fetch(self, symbol, instrument_id, timeframe, start_ms, end_ms) -> FetchResult:
        self.calls.append((start_ms, end_ms))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise ConnectionError("simulated provider outage")
        tf = timeframe_ms(timeframe)
        candles = [
            make_candle(t, tf=timeframe, price=100.0 + (t // tf) % 50 + self.price_shift,
                        instrument_id=instrument_id, source_symbol=symbol)
            for t in range(start_ms, end_ms, tf) if t not in self.missing
        ]
        if self.extra_unfinished:  # a provider that (wrongly) includes the in-progress bar
            candles.append(make_candle(end_ms, tf=timeframe, instrument_id=instrument_id))
        rejects = [RejectedRecord("simulated bad row", f"row {i}") for i in range(self.rejects_per_call)]
        return FetchResult(candles, rejects)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_instruments([CRYPTO, FX], 0)
    yield s
    s.close()


class FixedClock:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms
