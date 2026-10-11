# 04 — System architecture, layout and storage (XAU/USD only)

Scope: see doc 00. Every component below handles exactly one instrument, XAUUSD; the machinery stays generic only where that costs nothing.

## Target architecture (all phases)

```
                 ┌──────────────────────── Research plane (no trading rights) ─────────────────────────┐
 Broker/data ─▶  │ Market Data Engineer ──▶ Research DB ◀── Feature/Indicator lib ◀── Regime/Session      │
 (MT5 exports, │   (ingest, validate)       │   ▲            Cost/Liquidity models     News/Fundamentals  │
  APIs, FRED)   │                            ▼   │                                                         │
                 │   Strategy Researcher ──▶ Hypothesis registry ──▶ Backtest/Validation ──▶ Experiment    │
                 │                                                                            ledger       │
                 │   Coordinator (routes, reviews, never edits)  ◀── Findings (structured JSON)            │
                 └──────────────────────────────────────────────┬──────────────────────────────────────────┘
                                                                │ approved strategy version (human sign-off)
                 ┌──────────────────── Execution plane (Phase 5+, separate process & credentials) ─────────┐
                 │ Strategy runner ──TradeProposal──▶ Deterministic RISK GATE ──▶ Broker/exchange adapter   │
                 │                                    (limits, kill switch)        (trade-only keys)        │
                 └──────────────────────────────────────────────────────────────────────────────────────────┘
                   Reliability: supervisor, heartbeats, alerts, backups, audit log (spans both planes)
```

The research plane and execution plane are separate processes with separate credentials. The research plane never
holds keys that can trade. Phase 1 implemented the data-specialist core and the research DB. Execution (Phase 7 demo, Phase 8
live) runs only after doc 08's gates.

## Safety boundaries in Phase 1 (enforced by tests)
- HTTP is GET-only over HTTPS to an allowlisted host (`ReadOnlyHttpClient`). There is no method for sending a body
  or authentication header.
- Instrument scope is enforced at config load (`[scope] authorised_instruments = ["XAUUSD"]`).
- The dormant Binance reference provider points at a market-data-only host and is not configured.
- The source tree is scanned for order endpoints, signing code, credential names and write HTTP verbs. Any hit fails the suite.
- Config files are scanned for key, secret or token assignments.

## Directory structure

Current layout (✓) and planned additions (·):

```
atlas.toml                      ✓ settings, providers (no secrets)
config/instruments.toml         ✓ instrument registry
atlas/
  models.py                     ✓ Candle, enums (volume kind, price basis, data label)
  sessions.py                   ✓ xau_cfd (daily break + weekend)   · broker-hours reconciliation, holidays
  validation.py                 ✓ candle + series rules
  storage.py                    ✓ SQLite schema + store
  ingest.py                     ✓ chunked, resumable ingestion
  http.py                       ✓ read-only HTTP client
  providers/                    ✓ csv_file (MT5 exports)            · MT5 tick import, broker API; binance_spot (dormant)
  cli.py, logging_setup.py      ✓
  brokers/                      · versioned XAUUSD symbol specification records
  features/                     · indicator, structure, session, regime features (doc 06)
  macro/                        · point-in-time FRED/ALFRED series, release calendar, events
  research/                     · hypothesis registry, experiment ledger
  backtest/                     · event-driven engine, cost models
  risk/                         · deterministic risk gate (separate package before execution exists)
scripts/                        ✓ generate_sample_data.py
data/sample/                    ✓ synthetic sample + README
data/imports/                   ✓ user exports (git-ignored)
tests/                          ✓ unit, integration, live (opt-in), safety
docs/                           ✓ this documentation
```

## Storage decision

**SQLite now, with a schema that can move to PostgreSQL/TimescaleDB later.** Parquet will be added for bulk
feature matrices.

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **SQLite (WAL)** | zero setup; one file; transactional; stdlib; easy to snapshot and hash | single writer; slower beyond ~10⁸ rows | **Phase 1–3** |
| PostgreSQL + TimescaleDB | concurrency, compression, continuous aggregates | a server to run and secure | when 24/7 multi-process ingestion starts (Phase 6) |
| Parquet + DuckDB | very fast columnar analytics | no transactional upserts | Phase 2+ for analytics exports, not the system of record |

Volume estimate for XAUUSD: M1 ≈ 23 h × 5 d × 52 w × 60 ≈ 360k bars/year, so 10 years ≈ 3.6M rows, which SQLite handles comfortably.
Tick data with bid/ask is far larger (on the order of tens of millions of ticks per year, broker-dependent). Ticks will go to
partitioned Parquet files referenced from SQLite, not into the candles table (decision D5).

Prices are stored as IEEE-754 doubles (`REAL`). Doubles represent every source price exactly to the precision supplied
(≤ 8 decimals at these magnitudes), and arithmetic precision is a concern for later layers, not storage.

## Implemented schema (v1)

See `atlas/storage.py` for the DDL. Key points:
- `candles` primary key: `(instrument_id, provider, venue, timeframe, open_time_utc)`. Times are integer ms UTC, with
  open inclusive and close exclusive. Columns: OHLC, `price_basis`, `volume` plus `volume_kind`, `quote_volume`, `trade_count`,
  optional bid/ask OHLC, `source_symbol`, `data_label`, `validation_status` plus issue codes, `ingestion_run_id`,
  `ingested_at_utc`.
- `ingestion_runs`: request, status (`running/succeeded/failed/abandoned`), checkpoint, counters, error, software
  version, `resumed_from_run_id`.
- `candle_conflicts`: stored vs incoming values when a source revises history. The stored row is kept.
- `rejected_records`: unparseable source rows, verbatim.
- `quality_reports`: series validation summaries (JSON) per range and run.
- `instruments`: registry snapshot. `schema_meta`: schema version (mismatch blocks startup).
- `candles_readable` view: ISO-8601 timestamps for humans.

## Planned research-ledger schema (Phase 2–3; designed, not implemented)

```sql
CREATE TABLE broker_specs (spec_id TEXT PRIMARY KEY, broker TEXT, server TEXT, symbol TEXT,
  captured_at_utc INTEGER, contract_size REAL, digits INTEGER, tick_size REAL, volume_min REAL, volume_step REAL,
  stops_level_points INTEGER, freeze_level_points INTEGER, margin_json TEXT, swap_json TEXT, hours_json TEXT,
  server_tz TEXT, source_file_sha256 TEXT);    -- a new row whenever the broker changes anything
CREATE TABLE feature_definitions (feature_id TEXT PRIMARY KEY, name TEXT, version INTEGER, params_json TEXT,
  code_ref TEXT, warmup_bars INTEGER, created_at_utc INTEGER, UNIQUE(name, version, params_json));
CREATE TABLE events (event_id TEXT PRIMARY KEY, source TEXT, source_url TEXT, category TEXT, entity TEXT,
  scheduled_at_utc INTEGER, published_at_utc INTEGER, first_seen_at_utc INTEGER NOT NULL,
  fact_json TEXT NOT NULL, interpretation TEXT, vintage INTEGER NOT NULL DEFAULT 1, supersedes TEXT);
CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY, registered_at_utc INTEGER NOT NULL,
  spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL, author_agent TEXT, supersedes TEXT,
  status TEXT CHECK (status IN ('registered','testing','rejected','accepted_for_paper','retired')));
CREATE TABLE data_snapshots (snapshot_id TEXT PRIMARY KEY, created_at_utc INTEGER, query_json TEXT,
  row_count INTEGER, content_sha256 TEXT NOT NULL);       -- hash of the exact candle rows used
CREATE TABLE cost_models (cost_model_id TEXT PRIMARY KEY, version INTEGER, spec_json TEXT, evidence TEXT);
CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES hypotheses,
  kind TEXT CHECK (kind IN ('train','validation','walk_forward','test','sensitivity','baseline')),
  config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, snapshot_id TEXT NOT NULL REFERENCES data_snapshots,
  cost_model_id TEXT REFERENCES cost_models, assumptions TEXT NOT NULL, software_version TEXT NOT NULL,
  started_at_utc INTEGER, finished_at_utc INTEGER, status TEXT, outcome TEXT, error TEXT);
CREATE TABLE experiment_metrics (experiment_id TEXT REFERENCES experiments, metric TEXT, value REAL,
  PRIMARY KEY (experiment_id, metric));
CREATE TABLE experiment_trades (experiment_id TEXT, seq INTEGER, instrument_id TEXT, side TEXT,
  entry_time_utc INTEGER, entry_price REAL, exit_time_utc INTEGER, exit_price REAL, costs REAL, pnl REAL,
  PRIMARY KEY (experiment_id, seq));
CREATE TABLE strategy_versions (strategy_id TEXT, version INTEGER, hypothesis_id TEXT, spec_sha256 TEXT,
  approved_by TEXT, approved_at_utc INTEGER, PRIMARY KEY (strategy_id, version));
CREATE TABLE agent_messages (message_id TEXT PRIMARY KEY, agent TEXT, type TEXT, payload_json TEXT,
  valid INTEGER, errors TEXT, created_at_utc INTEGER);
CREATE TABLE test_set_access (experiment_id TEXT, hypothesis_id TEXT, accessed_at_utc INTEGER);  -- each look counts
-- Ledger tables are append-only: there are no DELETE paths, and failed or rejected experiments stay.
```
