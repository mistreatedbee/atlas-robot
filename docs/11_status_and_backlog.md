# 11 — Project status and prioritised backlog

*Owner: CTO / delivery lead. Updated at the end of every development cycle. Last update: 2026-10-11.*

## Current state (evidence, not intentions)

| Area | State | Evidence |
|---|---|---|
| Scope guard (XAUUSD only), no-trading boundary | Done | `test_config_and_safety.py` (scans for order/credential/MT5 trading calls) |
| Ingestion, validation, storage (schema v2 + migration) | Done | `test_ingest.py`, `test_storage.py`, `test_validation.py`, migration tests |
| Gold session calendar (`xau_cfd`) | Done; **broker hours unconfirmed** | `test_sessions.py` |
| Broker spec format (unverified / provisional_dev / verified) | Done | `test_broker_spec.py` |
| Vault Markets spec | **Unverified**: no numbers recorded | `config/brokers/vault_markets_xauusd.toml` |
| MT5 bar importer + server-time verification (both weekend sides) | Done, tested on **synthetic** MT5-format files only | `test_mt5_export.py`, `test_server_time.py` |
| MT5 tick parser, tick→bar reconciliation, spread profile | Done (functions); tick storage not built | `test_mt5_export.py` |
| Risk gate (fail-closed) + R500 feasibility CLI | Done; uses PROVISIONAL spec only | `test_risk_gate.py` |
| Indicators (SMA/EMA/Wilder/TR/ATR/RSI/BB/MACD/Stochastic/ADX/confirmed swings) + segments | Done | `test_indicators.py` (reference + look-ahead tests) |
| Simulation rules (docs/13 v2, independently challenged) | Done | review disposition table in docs/13 |
| Cost model + event-driven M1/M5 backtester (every entry through the risk gate, news windows, point-in-time USD/ZAR) | **Complete** (rules v2.3); **costs PROVISIONAL**; 3 independent reviews, 29 findings fixed | `test_backtest_engine.py` (hand-computed examples) |
| Experiment ledger (append-only, schema v4), reserved test windows per instrument, one look per hypothesis | Done | `test_ledger.py`, `test_runner.py` |
| US tier-1 event calendar (CPI, NFP, FOMC; official sources, point-in-time) | Done; used by the engine | `test_events.py` |
| Daily USD/ZAR (ECB reference rates, point-in-time) | Done; used by the engine | `test_fx.py` |
| Baselines (no-trade, matched random, session-long) + R10 leakage harness | Done | `test_baselines_leakage.py` |
| Health check (`atlas health`) | Done | `test_monitoring.py` |
| Read-only MT5 adapter (`atlas mt5-check`) | Built; tested with a FAKE terminal on Windows/macOS/Linux CI; **not yet run against the real Vault terminal** | `test_mt5_readonly.py` |
| Order preparation (no send capability) | Built | `test_orders.py` |
| Cross-platform (Windows, macOS, Linux; Python 3.11–3.13) | **Verified by GitHub Actions** (9/9 jobs green) | `.github/workflows/tests.yml` |
| Paper-trading executor (persistent duplicate-order journal, fail-closed fills, restart recovery, emergency stop, connection monitor) | Built | `test_paper.py` |
| Paper-trading session loop (strategy → gate → order preparation → paper fills) | Built; **identical to the backtester on the same data** | `test_paper_session.py` |
| Live execution | **Does not exist, by design** | No live mode in code; safety tests |

Tests: **392 passed, 2 skipped (live, opt-in)** locally; CI green on Windows, macOS and Linux.
Version control: git; remote https://github.com/mistreatedbee/atlas-robot (PUBLIC, by owner's decision 2026-10-11).

## Backlog (priority order)

| # | Item | Blocked? | Acceptance |
|---|---|---|---|
| 1 | Tick storage (Parquet, D5) + measured spread/slippage profile → replaces provisional cost values | Partly (needs Vault ticks for real values) | Round-trip + reconciliation tests; cost model built from data |
| 3 | First pre-registered gold hypotheses (momentum, pullback, range, breakout) | Needs real data to be meaningful | Pre-registration records; train/validation only until reviewed |
| 5 | Paper-trading simulator (live-like loop on recorded data) | No | Same decisions as the backtester on identical input |
| 6 | Broker integration research (MT5 read-only access options) | No (research only) | Written comparison; no execution code |
| 7 | **Real Vault data import and verified spec** | **Yes: owner's exports + support reply** | Phase 2 real-data acceptance in doc 08 |

## Blockers
- **Vault Markets data and specification** (backlog 7). Everything else proceeds without them. Provisional values are
  isolated in `config/brokers/PROVISIONAL_dev_xauusd.toml`, and any result produced with them is labelled `provisional_dev`.
