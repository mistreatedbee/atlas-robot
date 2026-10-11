"""Candles from local CSV files: the documented synthetic sample, or user-supplied exports.

Expected header: time,open,high,low,close[,volume][,bid_open,...,bid_close][,ask_open,...,ask_close]

`time` is the bar OPEN time. ISO-8601 values with an offset ("Z", "+02:00") are
converted directly. Naive values are interpreted in `source_timezone`; this is
how broker exports in server time (often UTC+2/+3) must be declared. Naive times
in a DST fall-back hour are ambiguous; they resolve to the first occurrence and
the series validator will flag the resulting duplicate.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import Candle, DataLabel, FetchResult, PriceBasis, RejectedRecord, VolumeKind, timeframe_ms

SIDE_FIELDS = ("open", "high", "low", "close")


def parse_time_ms(value: str, source_tz: ZoneInfo) -> int:
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=source_tz)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _opt_float(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None or value.strip() == "":
        return None
    return float(value)


class CsvFileProvider:
    max_bars_per_request = 10_000
    timeframes = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1"})

    def __init__(
        self,
        *,
        name: str,
        venue: str,
        path_template: str | Path,
        data_label: DataLabel,
        price_basis: PriceBasis,
        volume_kind: VolumeKind,
        source_timezone: str = "UTC",
    ):
        if data_label is DataLabel.LIVE_SOURCE:
            raise ValueError("a CSV file is never a live source; use user_file or synthetic_sample")
        self.name = name
        self.venue = venue
        self.path_template = str(path_template)
        self.data_label = data_label
        self.price_basis = price_basis
        self.volume_kind = volume_kind
        self.source_tz = ZoneInfo(source_timezone)
        self._cache: dict[tuple[str, str], FetchResult] = {}
        self._rejects_reported: set[tuple[str, str]] = set()

    def reset_run_state(self) -> None:
        """Called by ingest at the start of every run, so each run records its own rejects."""
        self._rejects_reported.clear()

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return Path(self.path_template.format(symbol=symbol, timeframe=timeframe))

    def _load(self, symbol: str, instrument_id: str, timeframe: str) -> FetchResult:
        key = (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]
        tf = timeframe_ms(timeframe)
        result = FetchResult()
        with self.path_for(symbol, timeframe).open(newline="") as fh:
            reader = csv.DictReader(fh)
            missing = {"time", *SIDE_FIELDS} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{self.path_for(symbol, timeframe)}: missing columns {sorted(missing)}")
            for line_no, row in enumerate(reader, start=2):
                raw = f"line {line_no}: " + ",".join(f"{k}={v}" for k, v in row.items())
                try:
                    open_time = parse_time_ms(row["time"], self.source_tz)
                    ohlc = [float(row[f]) for f in SIDE_FIELDS]
                    if not all(math.isfinite(v) for v in ohlc):
                        raise ValueError("non-finite price")
                    bid = [_opt_float(row, f"bid_{f}") for f in SIDE_FIELDS]
                    ask = [_opt_float(row, f"ask_{f}") for f in SIDE_FIELDS]
                    if self.price_basis is PriceBasis.BID and all(v is None for v in bid):
                        bid = list(ohlc)
                    if self.price_basis is PriceBasis.ASK and all(v is None for v in ask):
                        ask = list(ohlc)
                    volume = _opt_float(row, "volume")
                except (ValueError, TypeError, KeyError) as exc:
                    result.rejects.append(RejectedRecord(f"unparseable row: {exc}", raw))
                    continue
                result.candles.append(Candle(
                    instrument_id=instrument_id, provider=self.name, venue=self.venue,
                    timeframe=timeframe, source_symbol=symbol,
                    open_time=open_time, close_time=open_time + tf,
                    open=ohlc[0], high=ohlc[1], low=ohlc[2], close=ohlc[3],
                    price_basis=self.price_basis,
                    volume=volume, volume_kind=self.volume_kind if volume is not None else VolumeKind.NONE,
                    bid_open=bid[0], bid_high=bid[1], bid_low=bid[2], bid_close=bid[3],
                    ask_open=ask[0], ask_high=ask[1], ask_low=ask[2], ask_close=ask[3],
                ))
        self._cache[key] = result
        return result

    def fetch(self, symbol: str, instrument_id: str, timeframe: str, start_ms: int, end_ms: int) -> FetchResult:
        loaded = self._load(symbol, instrument_id, timeframe)
        # Rejected rows have no trustworthy timestamp, so they are reported once per file per provider instance.
        key = (symbol, timeframe)
        rejects = [] if key in self._rejects_reported else list(loaded.rejects)
        self._rejects_reported.add(key)
        return FetchResult(
            candles=[c for c in loaded.candles if start_ms <= c.open_time < end_ms],
            rejects=rejects,
        )
