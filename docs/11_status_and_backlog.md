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
| Cost model + event-driven M1/M5 backtester (every entry through the risk gate) | Done; **costs PROVISIONAL**; independent review in progress | `test_backtest_engine.py` (hand-computed examples) |
| Experiment ledger (append-only), splits with embargo, test-set guard | Done | `test_ledger.py`, `test_runner.py` |
| Baselines (no-trade, matched random, session-long) + R10 leakage harness | Done | `test_baselines_leakage.py` |
| Health check (`atlas health`) | Done | `test_monitoring.py` |
| Paper-trading simulator | Not started | — |
| Live execution | **Does not exist, by design** | No live mode in code; safety tests |

Tests: **284 passed, 1 skipped (live, opt-in)**; the live test passes when enabled.
Version control: local git only, no remote.

## Backlog (priority order)

| # | Item | Blocked? | Acceptance |
|---|---|---|---|
| 1 | Tick storage (Parquet, D5) + measured spread/slippage profile → replaces provisional cost values | Partly (needs Vault ticks for real values) | Round-trip + reconciliation tests; cost model built from data |
| 2 | Event calendar (FOMC/CPI/NFP official schedules, point-in-time) → news no-trade windows + slippage widening | No | Release-time gating tests; no event visible before its release |
| 3 | First pre-registered gold hypotheses (momentum, pullback, range, breakout) | Needs real data to be meaningful | Pre-registration records; train/validation only until reviewed |
| 4 | Daily USD/ZAR series for account conversion | No | Point-in-time FX applied per trade |
| 5 | Paper-trading simulator (live-like loop on recorded data) | No | Same decisions as the backtester on identical input |
| 6 | Broker integration research (MT5 read-only access options) | No (research only) | Written comparison; no execution code |
| 7 | **Real Vault data import and verified spec** | **Yes: owner's exports + support reply** | Phase 2 real-data acceptance in doc 08 |
| 8 | Paper-trading simulator (live-like loop on recorded data) | After 1–3 | Same decisions as backtester on identical input |
| 9 | Broker integration research (MT5 terminal API vs. EA bridge; read-only account access first) | No (research only) | Written comparison; no execution code |

## Blockers
- **Vault Markets data and specification** (backlog 7). Everything else proceeds without them. Provisional values are
  isolated in `config/brokers/PROVISIONAL_dev_xauusd.toml`, and any result produced with them is labelled `provisional_dev`.
