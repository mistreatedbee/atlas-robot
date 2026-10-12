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

SCHEMA_VERSION = 6

CANDLE_VALUE_COLUMNS = (
    "close_time_utc", "open", "high", "low", "close", "price_basis",
    "volume", "volume_kind", "quote_volume", "trade_count",
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close", "spread_points",
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
    software_version         TEXT NOT NULL,
    source_detail            TEXT          -- JSON: files + SHA-256, server-time model and its verification, etc.
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
    spread_points     INTEGER,            -- as reported by the source; NOT used as a cost until verified
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

-- ---- Experiment ledger (append-only; enforced by triggers) ----------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id        TEXT PRIMARY KEY,
    created_at_utc       INTEGER NOT NULL,
    finished_at_utc      INTEGER,
    name                 TEXT NOT NULL,
    strategy             TEXT NOT NULL,
    hypothesis_id        TEXT NOT NULL,     -- test-set access is limited per hypothesis, whatever the parameters
    period               TEXT NOT NULL CHECK (period IN ('train','validation','test','full')),
    status               TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    config_json          TEXT NOT NULL,     -- strategy params, engine rules, costs, risk limits, spec, data selection
    config_sha256        TEXT NOT NULL,
    data_selection_json  TEXT NOT NULL,
    data_sha256          TEXT,              -- hash of the exact candle rows used
    data_label           TEXT,
    cost_status          TEXT NOT NULL,     -- e.g. provisional_dev | verified
    spec_label           TEXT NOT NULL,
    code_version         TEXT NOT NULL,
    test_access_override INTEGER NOT NULL DEFAULT 0,
    look_key             TEXT,              -- config hash WITHOUT the cost multiplier: one test-set look
    cost_multiplier      REAL,
    metrics_json         TEXT,
    result_sha256        TEXT,
    error                TEXT
);
CREATE INDEX IF NOT EXISTS experiments_hypothesis ON experiments(hypothesis_id, period);

CREATE TABLE IF NOT EXISTS experiment_trades (
    experiment_id  TEXT NOT NULL REFERENCES experiments(experiment_id),
    seq            INTEGER NOT NULL,
    trade_json     TEXT NOT NULL,
    PRIMARY KEY (experiment_id, seq)
);

CREATE TABLE IF NOT EXISTS experiment_events (
    experiment_id  TEXT NOT NULL REFERENCES experiments(experiment_id),
    seq            INTEGER NOT NULL,
    bar_time_utc   INTEGER,
    kind           TEXT NOT NULL,       -- e.g. risk_rejected, expired_gap, ignored_in_position
    detail         TEXT,
    PRIMARY KEY (experiment_id, seq)
);

CREATE TRIGGER IF NOT EXISTS experiments_no_delete BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS experiments_finalised_immutable BEFORE UPDATE ON experiments
WHEN OLD.status != 'running'
BEGIN SELECT RAISE(ABORT, 'finished experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS experiments_config_immutable BEFORE UPDATE OF config_json, config_sha256, name,
    strategy, hypothesis_id, period, data_selection_json, created_at_utc, code_version, test_access_override,
    cost_status, spec_label, look_key, cost_multiplier ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment configuration is immutable'); END;
CREATE TRIGGER IF NOT EXISTS experiment_trades_only_while_running BEFORE INSERT ON experiment_trades
WHEN (SELECT status FROM experiments WHERE experiment_id = NEW.experiment_id) IS NOT 'running'
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only: experiment is not running'); END;
CREATE TRIGGER IF NOT EXISTS experiment_events_only_while_running BEFORE INSERT ON experiment_events
WHEN (SELECT status FROM experiments WHERE experiment_id = NEW.experiment_id) IS NOT 'running'
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only: experiment is not running'); END;
CREATE TRIGGER IF NOT EXISTS experiment_events_no_update BEFORE UPDATE ON experiment_events
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only'); END;

-- Test windows are reserved per data series the first time an experiment declares them; afterwards no
-- train/validation run may touch them. Rows are permanent.
CREATE TABLE IF NOT EXISTS reserved_test_windows (
    instrument_id  TEXT NOT NULL,
    provider       TEXT NOT NULL,
    venue          TEXT NOT NULL,
    timeframe      TEXT NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    reserved_by    TEXT NOT NULL,       -- hypothesis that first declared it
    reserved_at_utc INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, provider, venue, timeframe, start_ms, end_ms)
);
CREATE TRIGGER IF NOT EXISTS reserved_test_windows_no_delete BEFORE DELETE ON reserved_test_windows
BEGIN SELECT RAISE(ABORT, 'reserved test windows are permanent'); END;
-- ---- Ticks (bid/ask quotes from broker exports) ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS tick_imports (
    import_id        TEXT PRIMARY KEY,
    instrument_id    TEXT NOT NULL,
    provider         TEXT NOT NULL,
    venue            TEXT NOT NULL,
    file_name        TEXT NOT NULL,
    file_sha256      TEXT NOT NULL,
    server_time_model TEXT NOT NULL,
    server_time_basis TEXT NOT NULL,   -- which bar ingestion run verified the model
    rows_imported    INTEGER NOT NULL,
    rows_rejected    INTEGER NOT NULL,
    first_ms         INTEGER,
    last_ms          INTEGER,
    imported_at_ms   INTEGER NOT NULL,
    UNIQUE (provider, venue, file_sha256)
);
CREATE TABLE IF NOT EXISTS ticks (
    instrument_id TEXT NOT NULL,
    provider      TEXT NOT NULL,
    venue         TEXT NOT NULL,
    time_ms       INTEGER NOT NULL,   -- UTC
    seq           INTEGER NOT NULL,   -- order within the same millisecond, as in the source file
    bid           REAL NOT NULL CHECK (bid > 0),
    ask           REAL NOT NULL,
    import_id     TEXT NOT NULL REFERENCES tick_imports(import_id),
    PRIMARY KEY (instrument_id, provider, venue, time_ms, seq),
    CHECK (ask >= bid)
) WITHOUT ROWID;

-- ---- Paper execution (simulated; there is no live executor) -------------------------------------------
-- One row per client order id: the PRIMARY KEY is the duplicate-order guard, and it survives restarts.
CREATE TABLE IF NOT EXISTS order_journal (
    client_order_id TEXT PRIMARY KEY,
    venue           TEXT NOT NULL CHECK (venue IN ('paper')),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
    volume          REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('submitted','filled','rejected')),
    order_json      TEXT NOT NULL,
    reason          TEXT,
    fill_price      REAL,
    position_id     TEXT,
    created_ms      INTEGER NOT NULL,
    updated_ms      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id     TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE REFERENCES order_journal(client_order_id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    volume          REAL NOT NULL,
    entry_price     REAL NOT NULL,
    sl              REAL NOT NULL,
    tp              REAL,
    opened_ms       INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','closed')),
    exit_price      REAL,
    exit_reason     TEXT,
    closed_ms       INTEGER,
    gross_usd       REAL
);
CREATE TABLE IF NOT EXISTS paper_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    time_ms    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT
);
CREATE TRIGGER IF NOT EXISTS paper_events_no_update BEFORE UPDATE ON paper_events
BEGIN SELECT RAISE(ABORT, 'paper event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_events_no_delete BEFORE DELETE ON paper_events
BEGIN SELECT RAISE(ABORT, 'paper event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS order_journal_no_delete BEFORE DELETE ON order_journal
BEGIN SELECT RAISE(ABORT, 'order journal rows are permanent'); END;

CREATE TRIGGER IF NOT EXISTS reserved_test_windows_no_update BEFORE UPDATE ON reserved_test_windows
BEGIN SELECT RAISE(ABORT, 'reserved test windows are permanent'); END;
CREATE TRIGGER IF NOT EXISTS experiment_trades_no_delete BEFORE DELETE ON experiment_trades
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS experiment_trades_no_update BEFORE UPDATE ON experiment_trades
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS experiment_events_no_delete BEFORE DELETE ON experiment_events
BEGIN SELECT RAISE(ABORT, 'experiment ledger is append-only'); END;
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
        c.ask_open, c.ask_high, c.ask_low, c.ask_close, c.spread_points,
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

    # Forward-only migrations: version -> columns to add as (table, column, type). Each step checks the live
    # schema first, so a migration interrupted part-way can simply be re-run (idempotent).
    MIGRATIONS = {
        2: (("candles", "spread_points", "INTEGER"), ("ingestion_runs", "source_detail", "TEXT")),
        3: (),  # v3 only adds the experiment-ledger tables, created by the idempotent DDL below
        4: (("experiments", "look_key", "TEXT"), ("experiments", "cost_multiplier", "REAL"),
            "DROP TRIGGER IF EXISTS experiments_config_immutable"),  # recreated below with more frozen columns
        5: (),  # v5 adds the paper-execution tables (order journal, positions, fills), created by the DDL below
        6: (),  # v6 adds tick storage (ticks, tick_imports), created by the DDL below
    }

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
        row = (self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
               if existing else None)
        if row is None:
            with self.conn:
                self.conn.executescript(DDL)
                self.conn.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            return
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for step in self.MIGRATIONS[target]:
                    if isinstance(step, str):
                        self.conn.execute(step)
                        continue
                    table, column, coltype = step
                    existing_cols = self._columns(table)
                    # A table that does not exist yet is created below by the DDL, already with the column.
                    if existing_cols and column not in existing_cols:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                self.conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(target),))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        with self.conn:
            self.conn.executescript(DDL)  # idempotent: creates any missing tables/views

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

    def set_source_detail(self, run_id: str, detail: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE ingestion_runs SET source_detail=? WHERE run_id=?",
                              (json.dumps(detail, sort_keys=True, default=str), run_id))

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
            spread_points=r["spread_points"],
        )

    def data_labels_in(self, instrument_id: str, provider: str, venue: str, timeframe: str,
                       start_ms: int, end_ms: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT data_label FROM candles WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=? "
            "AND open_time_utc>=? AND open_time_utc<?", (instrument_id, provider, venue, timeframe, start_ms, end_ms))
        return {r[0] for r in rows}

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
