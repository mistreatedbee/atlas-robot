"""Binance Spot public klines via the market-data-only host data-api.binance.vision.

Verified 2026-10-11: no credentials required; limit<=1000 bars per request at
request weight 2; REQUEST_WEIGHT limit 6000/min per IP (from /api/v3/exchangeInfo);
trading endpoints return 404 on this host (checked by request). Kline row layout:
[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades,
 takerBuyBase, takerBuyQuote, ignore]; closeTime = openTime + interval - 1 ms.

Volume is exchange-reported traded quantity on Binance only, not market-wide volume.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from ..http import ReadOnlyHttpClient
from ..models import Candle, DataLabel, FetchResult, PriceBasis, RejectedRecord, VolumeKind, timeframe_ms

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data-api.binance.vision"
INTERVALS = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H4": "4h", "D1": "1d"}


class BinanceSpotProvider:
    venue = "binance"
    data_label = DataLabel.LIVE_SOURCE
    max_bars_per_request = 1000
    timeframes = frozenset(INTERVALS)

    def __init__(
        self,
        http: ReadOnlyHttpClient,
        *,
        name: str = "binance_spot",
        base_url: str = DEFAULT_BASE_URL,
        weight_limit_per_min: int = 6000,
        weight_safety_fraction: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.name = name
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.weight_budget = int(weight_limit_per_min * weight_safety_fraction)
        self._sleep = sleep
        self._wall_clock = wall_clock

    def _respect_weight(self, headers) -> None:
        used = headers.get("x-mbx-used-weight-1m")
        if used is not None and int(used) >= self.weight_budget:
            wait = 61 - (self._wall_clock() % 60)
            log.warning("request weight budget reached; pausing", extra={"used_weight_1m": int(used), "delay_s": wait})
            self._sleep(wait)

    def fetch(self, symbol: str, instrument_id: str, timeframe: str, start_ms: int, end_ms: int) -> FetchResult:
        if timeframe not in INTERVALS:
            raise ValueError(f"Binance provider does not support timeframe {timeframe}")
        tf = timeframe_ms(timeframe)
        params = {
            "symbol": symbol, "interval": INTERVALS[timeframe],
            "startTime": start_ms, "endTime": end_ms - 1, "limit": self.max_bars_per_request,
        }
        data, headers = self.http.get_json(f"{self.base_url}/api/v3/klines", params)
        self._respect_weight(headers)
        if not isinstance(data, list):
            raise ValueError(f"unexpected klines payload type {type(data).__name__}")

        result = FetchResult()
        for row in data:
            raw = json.dumps(row)
            try:
                open_time, close_time_incl = int(row[0]), int(row[6])
                if close_time_incl != open_time + tf - 1:
                    result.rejects.append(RejectedRecord(
                        f"closeTime {close_time_incl} inconsistent with interval {timeframe}", raw))
                    continue
                result.candles.append(Candle(
                    instrument_id=instrument_id, provider=self.name, venue=self.venue,
                    timeframe=timeframe, source_symbol=symbol,
                    open_time=open_time, close_time=open_time + tf,
                    open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                    price_basis=PriceBasis.TRADE,
                    volume=float(row[5]), volume_kind=VolumeKind.BASE_ASSET_TRADED,
                    quote_volume=float(row[7]), trade_count=int(row[8]),
                ))
            except (IndexError, TypeError, ValueError) as exc:
                result.rejects.append(RejectedRecord(f"unparseable kline: {exc}", raw))
        return result
