"""The interface every historical candle source implements."""

from __future__ import annotations

from typing import Protocol

from ..models import DataLabel, FetchResult


class CandleProvider(Protocol):
    name: str  # matches the key in atlas.toml [providers.*] and in instrument symbol maps
    venue: str  # where prices were formed, e.g. "binance" or a broker name
    data_label: DataLabel
    max_bars_per_request: int
    timeframes: frozenset[str]

    def fetch(self, symbol: str, instrument_id: str, timeframe: str, start_ms: int, end_ms: int) -> FetchResult:
        """Return candles whose open_time lies in [start_ms, end_ms), plus unparseable records.

        Providers must not filter out unfinished candles themselves; the ingestion
        pipeline does that against its own clock so the rule lives in one place.
        """
