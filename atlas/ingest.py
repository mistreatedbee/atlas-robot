"""Incremental, resumable historical candle ingestion.

Each request window is fetched in chunks. A chunk's candles, rejects and the run
checkpoint are committed in one transaction, so after a crash or error at most
the in-flight chunk is lost. Rerunning the identical request resumes from the
checkpoint; re-fetched candles are deduplicated by the store.

Unfinished candles (close_time later than the pipeline's clock) are never stored.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Instrument
from .models import timeframe_ms
from .providers.base import CandleProvider
from .sessions import get_calendar
from .storage import Store
from .validation import SeriesReport, status_for, validate_candle, validate_series

log = logging.getLogger(__name__)

Clock = Callable[[], int]


def system_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class IngestResult:
    run_id: str
    status: str
    resumed_from: str | None
    start_ms: int
    end_ms: int
    run: dict
    report: SeriesReport | None
    error: str | None = None


def _align_up(ts: int, tf: int) -> int:
    return -(-ts // tf) * tf


def ingest(
    store: Store,
    provider: CandleProvider,
    instrument: Instrument,
    timeframe: str,
    start_ms: int | None,
    end_ms: int,
    *,
    clock: Clock = system_clock_ms,
) -> IngestResult:
    """Ingest [start_ms, end_ms). With start_ms=None, continue after the latest stored candle."""
    tf = timeframe_ms(timeframe)
    if timeframe not in provider.timeframes:
        raise ValueError(f"provider {provider.name} does not support {timeframe}")
    symbol = instrument.symbol_for(provider.name)
    series_key = dict(instrument_id=instrument.instrument_id, provider=provider.name,
                      venue=provider.venue, timeframe=timeframe)

    if start_ms is None:
        latest = store.latest_open_time(**series_key)
        if latest is None:
            raise ValueError("no stored data for this series; an explicit start is required for the first ingestion")
        start_ms = latest + tf
    start_ms = _align_up(start_ms, tf)
    # Never request bars that cannot have closed yet; checkpoints must not move past unfinished data.
    end_ms = min(_align_up(end_ms, tf), clock() // tf * tf)
    if end_ms <= start_ms:
        raise ValueError("end must be after start")

    cursor, resumed_from = start_ms, None
    previous = store.find_resumable_run(**series_key, start_ms=start_ms, end_ms=end_ms)
    if previous is not None and previous["status"] in ("failed", "running"):
        store.mark_abandoned(previous["run_id"], clock())
        if previous["checkpoint_utc"] is not None:
            cursor, resumed_from = previous["checkpoint_utc"], previous["run_id"]

    run_id = store.start_run(**series_key, source_symbol=symbol, data_label=provider.data_label,
                             start_ms=start_ms, end_ms=end_ms, now_ms=clock(), resumed_from=resumed_from)
    ctx = {"run_id": run_id, **series_key, "symbol": symbol, "data_label": provider.data_label.value}
    log.info("ingestion started", extra={**ctx, "start_ms": start_ms, "end_ms": end_ms,
                                         "cursor_ms": cursor, "resumed_from": resumed_from})
    if resumed_from:
        log.info("resuming from checkpoint of earlier run", extra={**ctx, "resumed_from": resumed_from,
                                                                   "checkpoint_ms": cursor})

    status, error = "succeeded", None
    try:
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + provider.max_bars_per_request * tf)
            batch = provider.fetch(symbol, instrument.instrument_id, timeframe, cursor, chunk_end)
            now = clock()
            accepted, seen = [], set()
            extra = {"received": len(batch.candles), "unfinished_skipped": 0, "out_of_window": 0}
            for c in sorted(batch.candles, key=lambda c: c.open_time):
                if not (cursor <= c.open_time < chunk_end) or c.open_time in seen:
                    extra["out_of_window"] += 1
                    continue
                if c.close_time > now:
                    extra["unfinished_skipped"] += 1
                    continue
                seen.add(c.open_time)
                issues = validate_candle(c, now)
                accepted.append((c, status_for(issues), [i.code for i in issues]))
                for issue in issues:
                    log.warning("candle validation issue", extra={**ctx, "code": issue.code,
                                                                  "open_time_ms": issue.open_time,
                                                                  "detail": issue.message})
            counts = store.commit_chunk(run_id=run_id, data_label=provider.data_label, candles=accepted,
                                        rejects=batch.rejects, checkpoint_ms=chunk_end, now_ms=now,
                                        extra_counts=extra)
            for r in batch.rejects:
                log.warning("record rejected", extra={**ctx, "reason": r.reason, "raw": r.raw[:500]})
            if counts.conflicts:
                log.error("stored candles conflict with source; originals kept",
                          extra={**ctx, "conflicts": counts.conflicts})
            log.info("chunk committed", extra={**ctx, "chunk_start_ms": cursor, "chunk_end_ms": chunk_end,
                                               **extra, "inserted": counts.inserted,
                                               "duplicates": counts.duplicates})
            cursor = chunk_end
    except BaseException as exc:  # includes KeyboardInterrupt: record the failure, then propagate if fatal
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        store.finish_run(run_id, status=status, now_ms=clock(), error=error)
        log.error("ingestion failed; rerun the same command to resume from the checkpoint",
                  extra={**ctx, "error": error, "checkpoint_ms": cursor})
        if not isinstance(exc, Exception):
            raise
        return IngestResult(run_id, status, resumed_from, start_ms, end_ms, dict(store.get_run(run_id)), None, error)

    store.finish_run(run_id, status=status, now_ms=clock())
    report = validate_range(store, instrument, provider.name, provider.venue, timeframe, start_ms, end_ms,
                            run_id=run_id, clock=clock)
    run = dict(store.get_run(run_id))
    log.info("ingestion finished", extra={**ctx, "status": status, "inserted": run["inserted"],
                                          "duplicates": run["duplicates"], "rejected": run["rejected"],
                                          "quality_status": report.status.value})
    return IngestResult(run_id, status, resumed_from, start_ms, end_ms, run, report)


def validate_range(
    store: Store,
    instrument: Instrument,
    provider_name: str,
    venue: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    *,
    run_id: str | None = None,
    max_staleness_ms: int | None = None,
    clock: Clock = system_clock_ms,
) -> SeriesReport:
    """Run series validation over stored (non-invalid) candles and persist the quality report."""
    candles = store.get_candles(instrument.instrument_id, provider_name, timeframe, venue=venue,
                                start_ms=start_ms, end_ms=end_ms)
    now = clock()
    report = validate_series(candles, get_calendar(instrument.session_calendar), now,
                             max_staleness_ms=max_staleness_ms)
    invalid = store.count(
        "candles",
        "instrument_id=? AND provider=? AND venue=? AND timeframe=? AND open_time_utc>=? AND open_time_utc<? "
        "AND validation_status='invalid'",
        (instrument.instrument_id, provider_name, venue, timeframe, start_ms, end_ms),
    )
    summary = report.summary()
    summary["invalid_candles_excluded"] = invalid
    summary["data_labels"] = sorted(store.data_labels(instrument.instrument_id, provider_name, timeframe))
    store.save_quality_report(instrument_id=instrument.instrument_id, provider=provider_name, venue=venue,
                              timeframe=timeframe, start_ms=start_ms, end_ms=end_ms, run_id=run_id,
                              summary=summary, now_ms=now)
    return report
