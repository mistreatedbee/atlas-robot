"""MetaTrader 5 history exports (View -> Symbols -> Bars / Ticks -> Export).

Format as produced by MT5 (header-driven, so column order does not matter; to be confirmed against the
first real Vault Markets export, see docs/10):

  bars : <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>   e.g. 2026.10.05  01:00:00 ...
  ticks: <DATE> <TIME> <BID> <ASK> <LAST> <VOLUME> <FLAGS>                     e.g. 2026.10.05  01:00:00.123 ...

Delimiter is tab (comma accepted). Encoding is detected from the byte-order mark (UTF-16 or UTF-8).
Timestamps are naive SERVER time; they are converted with a declared ServerTimeModel that is first verified
against the weekend closes in the data itself. Import is refused, before anything is written, on a mismatch.

Bars from CFD symbols are normally built from BID prices; this is declared in config and checked against ticks.
`<TICKVOL>` is broker tick count (a proxy). `<VOL>` (real volume) is ignored: OTC gold has none.
`<SPREAD>` is stored as reported in `spread_points` and is NOT used as a trading cost until its meaning
(e.g. minimum vs average spread over the bar) has been verified against tick data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..brokers.server_time import InferenceReport, ServerTimeError, ServerTimeModel, infer_offsets, verify_model
from ..models import Candle, DataLabel, FetchResult, PriceBasis, RejectedRecord, VolumeKind, timeframe_ms

BAR_REQUIRED = {"<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>"}
TICK_REQUIRED = {"<DATE>", "<TIME>", "<BID>", "<ASK>"}


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _rows(text: str) -> tuple[list[str], Iterable[list[str]]]:
    first_line = text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [h.strip().upper() for h in next(reader, [])]
    return header, reader


def parse_server_time(date_s: str, time_s: str | None) -> datetime:
    date_s = date_s.strip()
    time_s = (time_s or "00:00:00").strip()
    fmt_time = "%H:%M:%S.%f" if "." in time_s else ("%H:%M:%S" if time_s.count(":") == 2 else "%H:%M")
    return datetime.strptime(f"{date_s} {time_s}", f"%Y.%m.%d {fmt_time}")


@dataclass(frozen=True)
class ServerBar:
    server_open: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int | None
    spread_points: int | None
    raw: str


def parse_bars(text: str, source: str) -> tuple[list[ServerBar], list[RejectedRecord]]:
    header, reader = _rows(text)
    missing = BAR_REQUIRED - set(header)
    if missing:
        raise ValueError(f"{source}: not an MT5 bar export; missing columns {sorted(missing)} in header {header}")
    idx = {h: i for i, h in enumerate(header)}
    bars, rejects = [], []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        raw = f"{source}:{line_no}: " + "\t".join(row)
        try:
            prices = [float(row[idx[k]]) for k in ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>")]
            if not all(math.isfinite(p) for p in prices):
                raise ValueError("non-finite price")
            tickvol = int(row[idx["<TICKVOL>"]]) if "<TICKVOL>" in idx and row[idx["<TICKVOL>"]].strip() else None
            spread = int(row[idx["<SPREAD>"]]) if "<SPREAD>" in idx and row[idx["<SPREAD>"]].strip() else None
            ts = parse_server_time(row[idx["<DATE>"]], row[idx["<TIME>"]] if "<TIME>" in idx else None)
        except (ValueError, IndexError) as exc:
            rejects.append(RejectedRecord(f"unparseable MT5 bar row: {exc}", raw))
            continue
        bars.append(ServerBar(ts, *prices, tickvol, spread, raw))
    return bars, rejects


class Mt5ExportProvider:
    """CandleProvider over one or more MT5 bar-export files for a symbol/timeframe."""

    max_bars_per_request = 50_000
    timeframes = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1"})

    def __init__(self, *, name: str, venue: str, directory: str | Path, server_time_model: str,
                 data_label: DataLabel = DataLabel.USER_FILE, price_basis: PriceBasis = PriceBasis.BID,
                 file_pattern: str = "{symbol}_{timeframe}_bars_*.csv"):
        if data_label is DataLabel.LIVE_SOURCE:
            raise ValueError("an exported file is never a live source")
        self.name = name
        self.venue = venue
        self.directory = Path(directory)
        self.model = ServerTimeModel(server_time_model)
        self.data_label = data_label
        self.price_basis = price_basis
        self.file_pattern = file_pattern
        self._cache: dict[tuple[str, str], FetchResult] = {}
        self._details: dict[tuple[str, str], dict] = {}
        self._rejects_reported: set[tuple[str, str]] = set()

    def files_for(self, symbol: str, timeframe: str) -> list[Path]:
        files = sorted(self.directory.glob(self.file_pattern.format(symbol=symbol, timeframe=timeframe)))
        if not files:
            raise FileNotFoundError(
                f"no MT5 exports matching {self.file_pattern.format(symbol=symbol, timeframe=timeframe)} "
                f"in {self.directory}")
        return files

    def _load(self, symbol: str, instrument_id: str, timeframe: str) -> FetchResult:
        key = (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]
        tf = timeframe_ms(timeframe)
        files = self.files_for(symbol, timeframe)
        all_bars: list[ServerBar] = []
        result = FetchResult()
        file_info = []
        for f in files:
            bars, rejects = parse_bars(read_text(f), f.name)
            all_bars.extend(bars)
            result.rejects.extend(rejects)
            file_info.append({"file": f.name, "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                              "rows": len(bars), "rejected": len(rejects)})

        report: InferenceReport = infer_offsets([b.server_open for b in all_bars], tf)
        problems = verify_model(self.model, report)
        detail = {
            "files": file_info,
            "server_time_model": self.model.spec,
            "weekend_observations": [
                {"server_close": o.server_close.isoformat(), "expected_close_utc": o.expected_close_utc.isoformat(),
                 "inferred_offset_h": None if o.inferred_offset is None else o.inferred_offset.total_seconds() / 3600,
                 "residual_min": o.residual_minutes} for o in report.observations],
            "server_time_verified": not problems,
            "price_basis": self.price_basis.value,
            "data_label": self.data_label.value,
        }
        self._details[key] = detail
        if problems:
            raise ServerTimeError("server-time model rejected by the data; nothing imported: " + "; ".join(problems))

        by_time: dict[int, Candle] = {}
        for b in all_bars:
            try:
                open_ms = self.model.to_utc_ms(b.server_open)
            except ServerTimeError as exc:
                result.rejects.append(RejectedRecord(str(exc), b.raw))
                continue
            bid = (b.open, b.high, b.low, b.close) if self.price_basis is PriceBasis.BID else (None,) * 4
            candle = Candle(
                instrument_id=instrument_id, provider=self.name, venue=self.venue, timeframe=timeframe,
                source_symbol=symbol, open_time=open_ms, close_time=open_ms + tf,
                open=b.open, high=b.high, low=b.low, close=b.close, price_basis=self.price_basis,
                volume=float(b.tick_volume) if b.tick_volume is not None else None,
                volume_kind=VolumeKind.TICK_COUNT if b.tick_volume is not None else VolumeKind.NONE,
                bid_open=bid[0], bid_high=bid[1], bid_low=bid[2], bid_close=bid[3],
                spread_points=b.spread_points,
            )
            existing = by_time.get(open_ms)
            if existing is None:
                by_time[open_ms] = candle
            elif existing.values_key() != candle.values_key():
                result.rejects.append(RejectedRecord(
                    "conflicting duplicate bar across export files; first occurrence kept", b.raw))
            # identical duplicates from overlapping exports are dropped silently
        result.candles = sorted(by_time.values(), key=lambda c: c.open_time)
        self._cache[key] = result
        return result

    def describe(self, symbol: str, timeframe: str) -> dict:
        """Provenance for the ingestion run: files, hashes, server-time verification (kept even on refusal)."""
        if (symbol, timeframe) not in self._details:
            self._load(symbol, symbol, timeframe)
        return self._details[(symbol, timeframe)]

    def fetch(self, symbol: str, instrument_id: str, timeframe: str, start_ms: int, end_ms: int) -> FetchResult:
        loaded = self._load(symbol, instrument_id, timeframe)
        key = (symbol, timeframe)
        rejects = [] if key in self._rejects_reported else list(loaded.rejects)
        self._rejects_reported.add(key)
        return FetchResult([c for c in loaded.candles if start_ms <= c.open_time < end_ms], rejects)


# ---- ticks ---------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Tick:
    time_ms: int
    bid: float
    ask: float


def parse_ticks(text: str, source: str, model: ServerTimeModel) -> tuple[list[Tick], list[RejectedRecord]]:
    """Parse an MT5 tick export. MT5 writes a tick row when either side changes and may leave the other side
    empty; the quote state is the last known bid and ask, so the missing side is carried forward. Rows before
    both sides have been seen are rejected (there is no known quote yet)."""
    header, reader = _rows(text)
    missing = TICK_REQUIRED - set(header)
    if missing:
        raise ValueError(f"{source}: not an MT5 tick export; missing columns {sorted(missing)}")
    idx = {h: i for i, h in enumerate(header)}
    ticks, rejects = [], []
    bid = ask = None
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        raw = f"{source}:{line_no}: " + "\t".join(row)
        try:
            ts = model.to_utc_ms(parse_server_time(row[idx["<DATE>"]], row[idx["<TIME>"]]))
            b, a = row[idx["<BID>"]].strip(), row[idx["<ASK>"]].strip()
            bid = float(b) if b else bid
            ask = float(a) if a else ask
        except (ValueError, IndexError, ServerTimeError) as exc:
            rejects.append(RejectedRecord(f"unparseable MT5 tick row: {exc}", raw))
            continue
        if bid is None or ask is None:
            rejects.append(RejectedRecord("tick before both bid and ask are known", raw))
            continue
        if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
            rejects.append(RejectedRecord(f"invalid quote bid={bid} ask={ask}", raw))
            continue
        ticks.append(Tick(ts, bid, ask))
    return ticks, rejects


@dataclass(frozen=True)
class BarMismatch:
    open_time: int
    field: str
    from_ticks: float
    from_bars: float


def aggregate_bid_bars(ticks: Iterable[Tick], tf_ms: int) -> dict[int, tuple[float, float, float, float]]:
    """Bid OHLC per bar from ticks (ticks must be in time order)."""
    bars: dict[int, list[float]] = {}
    for t in ticks:
        start = t.time_ms // tf_ms * tf_ms
        b = bars.get(start)
        if b is None:
            bars[start] = [t.bid, t.bid, t.bid, t.bid]
        else:
            b[1] = max(b[1], t.bid)
            b[2] = min(b[2], t.bid)
            b[3] = t.bid
    return {k: tuple(v) for k, v in bars.items()}


def reconcile_ticks_with_bars(ticks: list[Tick], candles: list[Candle], tolerance: float) -> list[BarMismatch]:
    """Check that bars exported by MT5 equal bid bars rebuilt from its ticks, for bars covered by both."""
    if not candles:
        return []
    tf = candles[0].close_time - candles[0].open_time
    rebuilt = aggregate_bid_bars(ticks, tf)
    out = []
    for c in candles:
        r = rebuilt.get(c.open_time)
        if r is None:
            continue
        for name, a, b in zip(("open", "high", "low", "close"), r, (c.open, c.high, c.low, c.close)):
            if abs(a - b) > tolerance:
                out.append(BarMismatch(c.open_time, name, a, b))
    return out


def spread_profile(ticks: Iterable[Tick]) -> dict[int, dict[str, float]]:
    """Spread (ask - bid, USD/oz) by UTC hour: count, median, p90, max. Tick-weighted, not time-weighted."""
    by_hour: dict[int, list[float]] = {}
    for t in ticks:
        by_hour.setdefault(t.time_ms // 3_600_000 % 24, []).append(t.ask - t.bid)
    out = {}
    for hour, values in sorted(by_hour.items()):
        values.sort()
        n = len(values)
        out[hour] = {"count": n, "median": values[n // 2], "p90": values[min(n - 1, int(0.9 * n))], "max": values[-1]}
    return out
