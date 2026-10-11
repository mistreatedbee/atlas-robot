"""SQLite research store: instruments, candles, ingestion runs, conflicts, rejects, quality reports.

Write policy for an already-stored candle key (instrument, provider, venue, timeframe, open_time):
  * identical values  -> counted as a duplicate, nothing written;
  * different values  -> the stored row is KEPT and both versions are written to
    candle_conflicts for review. Stored history never changes silently, so an
    experiment rerun over the same range reads the same data.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import Instrument
from .models import Candle, DataLabel, PriceBasis, RejectedRecord, ValidationStatus, VolumeKind

SCHEMA_VERSION = 1

CANDLE_VALUE_COLUMNS = (
    "close_time_utc", "open", "high", "low", "close", "price_basis",
    "volume", "volume_kind", "quote_volume", "trade_count",
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close",
)

DDL = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id    TEXT PRIMARY KEY,
    asset_class      TEXT NOT NULL,
    base_currency    TEXT NOT NULL,
    quote_currency   TEXT NOT NULL,
    session_calendar TEXT NOT NULL,
    config_json      TEXT NOT NULL,
    updated_at_utc   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id                   TEXT PRIMARY KEY,
    instrument_id            TEXT NOT NULL REFERENCES instruments(instrument_id),
    provider                 TEXT NOT NULL,
    venue                    TEXT NOT NULL,
    timeframe                TEXT NOT NULL,
    source_symbol            TEXT NOT NULL,
    data_label               TEXT NOT NULL,
    requested_start_utc      INTEGER NOT NULL,
    requested_end_utc        INTEGER NOT NULL,
    resumed_from_run_id      TEXT,
    status                   TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','abandoned')),
    started_at_utc           INTEGER NOT NULL,
    finished_at_utc          INTEGER,
    checkpoint_utc           INTEGER,   -- every bar with open_time < checkpoint has been fetched and committed
    received                 INTEGER NOT NULL DEFAULT 0,
    inserted                 INTEGER NOT NULL DEFAULT 0,
    duplicates               INTEGER NOT NULL DEFAULT 0,
    conflicts                INTEGER NOT NULL DEFAULT 0,
    rejected                 INTEGER NOT NULL DEFAULT 0,
    unfinished_skipped       INTEGER NOT NULL DEFAULT 0,
    out_of_window            INTEGER NOT NULL DEFAULT 0,
    invalid                  INTEGER NOT NULL DEFAULT 0,
    error                    TEXT,
    software_version         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
    instrument_id     TEXT NOT NULL REFERENCES instruments(instrument_id),
    provider          TEXT NOT NULL,
    venue             TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    open_time_utc     INTEGER NOT NULL,   -- ms since epoch, inclusive
    close_time_utc    INTEGER NOT NULL,   -- ms since epoch, exclusive
    open              REAL NOT NULL,
    high              REAL NOT NULL,
    low               REAL NOT NULL,
    close             REAL NOT NULL,
    price_basis       TEXT NOT NULL CHECK (price_basis IN ({", ".join(repr(p.value) for p in PriceBasis)})),
    volume            REAL,
    volume_kind       TEXT NOT NULL CHECK (volume_kind IN ({", ".join(repr(v.value) for v in VolumeKind)})),
    quote_volume      REAL,
    trade_count       INTEGER,
    bid_open REAL, bid_high REAL, bid_low REAL, bid_close REAL,
    ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
    source_symbol     TEXT NOT NULL,
    data_label        TEXT NOT NULL CHECK (data_label IN ({", ".join(repr(d.value) for d in DataLabel)})),
    validation_status TEXT NOT NULL CHECK (validation_status IN ({", ".join(repr(s.value) for s in ValidationStatus)})),
    validation_issues TEXT NOT NULL,      -- JSON list of issue codes
    ingestion_run_id  TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    ingested_at_utc   INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, provider, venue, timeframe, open_time_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS candle_conflicts (
    conflict_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id   TEXT NOT NULL,
    provider        TEXT NOT NULL,
    venue           TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    open_time_utc   INTEGER NOT NULL,
    stored_json     TEXT NOT NULL,
    incoming_json   TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    detected_at_utc INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_records (
    reject_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    reason          TEXT NOT NULL,
    raw             TEXT NOT NULL,
    detected_at_utc INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_reports (
    report_id        TEXT PRIMARY KEY,
    created_at_utc   INTEGER NOT NULL,
    instrument_id    TEXT NOT NULL,
    provider         TEXT NOT NULL,
    venue            TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    range_start_utc  INTEGER NOT NULL,
    range_end_utc    INTEGER NOT NULL,
    run_id           TEXT,
    status           TEXT NOT NULL,
    summary_json     TEXT NOT NULL,
    software_version TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS candles_readable AS
SELECT instrument_id, provider, venue, timeframe,
       strftime('%Y-%m-%dT%H:%M:%SZ', open_time_utc / 1000, 'unixepoch')  AS open_time,
       strftime('%Y-%m-%dT%H:%M:%SZ', close_time_utc / 1000, 'unixepoch') AS close_time,
       open, high, low, close, price_basis, volume, volume_kind,
       data_label, validation_status, validation_issues, ingestion_run_id
FROM candles;
"""


@dataclass
class WriteCounts:
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid: int = 0


def _candle_row_values(c: Candle) -> tuple:
    return (
        c.close_time, c.open, c.high, c.low, c.close, c.price_basis.value,
        c.volume, c.volume_kind.value, c.quote_volume, c.trade_count,
        c.bid_open, c.bid_high, c.bid_low, c.bid_close,
        c.ask_open, c.ask_high, c.ask_low, c.ask_close,
    )


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        with self.conn:
            self.conn.executescript(DDL)
            row = self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(f"database schema v{row['value']} != code schema v{SCHEMA_VERSION}; migration required")

    # ---- instruments -------------------------------------------------------------------------

    def upsert_instruments(self, instruments: Iterable[Instrument], now_ms: int) -> None:
        with self.conn:
            self.conn.executemany(
                """INSERT INTO instruments VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(instrument_id) DO UPDATE SET asset_class=excluded.asset_class,
                     base_currency=excluded.base_currency, quote_currency=excluded.quote_currency,
                     session_calendar=excluded.session_calendar, config_json=excluded.config_json,
                     updated_at_utc=excluded.updated_at_utc""",
                [(i.instrument_id, i.asset_class, i.base_currency, i.quote_currency, i.session_calendar,
                  json.dumps(i.as_dict(), sort_keys=True), now_ms) for i in instruments],
            )

    # ---- ingestion runs ----------------------------------------------------------------------

    def start_run(self, *, instrument_id: str, provider: str, venue: str, timeframe: str, source_symbol: str,
                  data_label: DataLabel, start_ms: int, end_ms: int, now_ms: int,
                  resumed_from: str | None = None) -> str:
        run_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                """INSERT INTO ingestion_runs (run_id, instrument_id, provider, venue, timeframe, source_symbol,
                     data_label, requested_start_utc, requested_end_utc, resumed_from_run_id, status,
                     started_at_utc, checkpoint_utc, software_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'running',?,?,?)""",
                (run_id, instrument_id, provider, venue, timeframe, source_symbol, data_label.value,
                 start_ms, end_ms, resumed_from, now_ms, None, __version__),
            )
        return run_id

    def find_resumable_run(self, *, instrument_id: str, provider: str, venue: str, timeframe: str,
                           start_ms: int, end_ms: int) -> sqlite3.Row | None:
        """Most recent unfinished run (failed, or left 'running' by a crash) for the identical request."""
        return self.conn.execute(
            """SELECT * FROM ingestion_runs
               WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=?
                 AND requested_start_utc=? AND requested_end_utc=?
               ORDER BY started_at_utc DESC, rowid DESC LIMIT 1""",
            (instrument_id, provider, venue, timeframe, start_ms, end_ms),
        ).fetchone()

    def mark_abandoned(self, run_id: str, now_ms: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE ingestion_runs SET status='abandoned', finished_at_utc=?, "
                "error=COALESCE(error, 'process ended without finishing the run') "
                "WHERE run_id=? AND status='running'",
                (now_ms, run_id),
            )

    def finish_run(self, run_id: str, *, status: str, now_ms: int, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE ingestion_runs SET status=?, finished_at_utc=?, error=? WHERE run_id=?",
                (status, now_ms, error, run_id),
            )

    def get_run(self, run_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY started_at_utc DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- candles -----------------------------------------------------------------------------

    def commit_chunk(
        self,
        *,
        run_id: str,
        data_label: DataLabel,
        candles: Sequence[tuple[Candle, ValidationStatus, list[str]]],
        rejects: Sequence[RejectedRecord],
        checkpoint_ms: int,
        now_ms: int,
        extra_counts: dict[str, int],
    ) -> WriteCounts:
        """Write one chunk atomically: candles, conflicts, rejects and the run checkpoint together."""
        counts = WriteCounts()
        insert_sql = f"""INSERT OR IGNORE INTO candles (instrument_id, provider, venue, timeframe, open_time_utc,
                {", ".join(CANDLE_VALUE_COLUMNS)}, source_symbol, data_label, validation_status,
                validation_issues, ingestion_run_id, ingested_at_utc)
            VALUES ({", ".join("?" * (5 + len(CANDLE_VALUE_COLUMNS) + 6))})"""
        with self.conn:
            for c, status, issue_codes in candles:
                key = (c.instrument_id, c.provider, c.venue, c.timeframe, c.open_time)
                values = _candle_row_values(c)
                cur = self.conn.execute(
                    insert_sql,
                    (*key, *values, c.source_symbol, data_label.value, status.value,
                     json.dumps(issue_codes), run_id, now_ms),
                )
                if cur.rowcount == 1:
                    counts.inserted += 1
                    counts.invalid += status is ValidationStatus.INVALID
                    continue
                stored = self.conn.execute(
                    f"SELECT {', '.join(CANDLE_VALUE_COLUMNS)} FROM candles WHERE instrument_id=? AND provider=? "
                    "AND venue=? AND timeframe=? AND open_time_utc=?", key,
                ).fetchone()
                if tuple(stored) == values:
                    counts.duplicates += 1
                else:
                    counts.conflicts += 1
                    self.conn.execute(
                        "INSERT INTO candle_conflicts (instrument_id, provider, venue, timeframe, open_time_utc, "
                        "stored_json, incoming_json, run_id, detected_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                        (*key, json.dumps(dict(zip(CANDLE_VALUE_COLUMNS, tuple(stored)))),
                         json.dumps(dict(zip(CANDLE_VALUE_COLUMNS, values))), run_id, now_ms),
                    )
            self.conn.executemany(
                "INSERT INTO rejected_records (run_id, reason, raw, detected_at_utc) VALUES (?,?,?,?)",
                [(run_id, r.reason, r.raw, now_ms) for r in rejects],
            )
            self.conn.execute(
                """UPDATE ingestion_runs SET checkpoint_utc=?, received=received+?, inserted=inserted+?,
                     duplicates=duplicates+?, conflicts=conflicts+?, rejected=rejected+?,
                     unfinished_skipped=unfinished_skipped+?, out_of_window=out_of_window+?, invalid=invalid+?
                   WHERE run_id=?""",
                (checkpoint_ms, extra_counts.get("received", 0), counts.inserted, counts.duplicates,
                 counts.conflicts, len(rejects), extra_counts.get("unfinished_skipped", 0),
                 extra_counts.get("out_of_window", 0), counts.invalid, run_id),
            )
        return counts

    def latest_open_time(self, instrument_id: str, provider: str, venue: str, timeframe: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(open_time_utc) FROM candles WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=?",
            (instrument_id, provider, venue, timeframe),
        ).fetchone()
        return row[0]

    def get_candles(
        self,
        instrument_id: str,
        provider: str,
        timeframe: str,
        *,
        venue: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        include_invalid: bool = False,
    ) -> list[Candle]:
        """Candles ordered by open_time. INVALID candles are excluded unless explicitly requested."""
        sql = "SELECT * FROM candles WHERE instrument_id=? AND provider=? AND timeframe=?"
        args: list = [instrument_id, provider, timeframe]
        if venue is not None:
            sql += " AND venue=?"
            args.append(venue)
        if start_ms is not None:
            sql += " AND open_time_utc>=?"
            args.append(start_ms)
        if end_ms is not None:
            sql += " AND open_time_utc<?"
            args.append(end_ms)
        if not include_invalid:
            sql += " AND validation_status != 'invalid'"
        sql += " ORDER BY open_time_utc"
        return [self._row_to_candle(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _row_to_candle(r: sqlite3.Row) -> Candle:
        return Candle(
            instrument_id=r["instrument_id"], provider=r["provider"], venue=r["venue"],
            timeframe=r["timeframe"], source_symbol=r["source_symbol"],
            open_time=r["open_time_utc"], close_time=r["close_time_utc"],
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            price_basis=PriceBasis(r["price_basis"]), volume=r["volume"], volume_kind=VolumeKind(r["volume_kind"]),
            quote_volume=r["quote_volume"], trade_count=r["trade_count"],
            bid_open=r["bid_open"], bid_high=r["bid_high"], bid_low=r["bid_low"], bid_close=r["bid_close"],
            ask_open=r["ask_open"], ask_high=r["ask_high"], ask_low=r["ask_low"], ask_close=r["ask_close"],
        )

    def data_labels(self, instrument_id: str, provider: str, timeframe: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT data_label FROM candles WHERE instrument_id=? AND provider=? AND timeframe=?",
            (instrument_id, provider, timeframe),
        )
        return {r[0] for r in rows}

    def count(self, table: str, where: str = "1=1", args: Sequence = ()) -> int:
        if table not in {"candles", "candle_conflicts", "rejected_records", "ingestion_runs", "quality_reports"}:
            raise ValueError(table)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]

    # ---- quality reports ---------------------------------------------------------------------

    def save_quality_report(self, *, instrument_id: str, provider: str, venue: str, timeframe: str,
                            start_ms: int, end_ms: int, run_id: str | None, summary: dict, now_ms: int) -> str:
        report_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                "INSERT INTO quality_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, now_ms, instrument_id, provider, venue, timeframe, start_ms, end_ms, run_id,
                 summary["status"], json.dumps(summary), __version__),
            )
        return report_id
