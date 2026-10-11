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
| Indicator core (SMA/EMA/Wilder/TR/ATR/RSI/BB/MACD) + segments | Done | `test_indicators.py` |
| Backtester, cost model, strategy framework, experiment ledger | Not started | — |
| Paper-trading simulator, monitoring, health checks | Not started | — |
| Live execution | **Does not exist, by design** | No live mode in code; safety tests |

Tests: **213 passed, 1 skipped (live, opt-in)**; the live test passes when enabled.
Version control: local git only, no remote.

## Backlog (priority order)

| # | Item | Blocked? | Acceptance |
|---|---|---|---|
| 1 | **Cost model + event-driven backtester** (M1/M5, bid bars + spread model, next-bar fills, conservative intrabar stop-first, broker constraints via spec, risk gate in the loop) | No | Hand-computed trade fixture reproduced exactly; costs ×1/×1.5/×2; every trade passes through `risk.gate.evaluate`; no look-ahead test |
| 2 | **Experiment ledger** (hypotheses, data snapshots with content hash, configs, results; append-only) | No | Re-running an experiment gives an identical result hash; failed experiments retained |
| 3 | Baseline strategies (random entry with matched exposure; "no trade") | No | Baselines reproducible with a fixed seed |
| 4 | Remaining features: ADX, stochastic, swing structure (confirmed swings), session features | No | Same reference + look-ahead tests as D3 |
| 5 | Tick storage (Parquet, decision D5) and spread profile report CLI | No (needs `pyarrow`) | Round-trip and reconciliation tests |
| 6 | Monitoring: structured health report (data freshness, last run status, rejects) | No | CLI health check with non-zero exit on stale/failed |
| 7 | **Real Vault data import and verified spec** | **Yes: owner's exports + support reply** | Phase 2 real-data acceptance in doc 08 |
| 8 | Paper-trading simulator (live-like loop on recorded data) | After 1–3 | Same decisions as backtester on identical input |
| 9 | Broker integration research (MT5 terminal API vs. EA bridge; read-only account access first) | No (research only) | Written comparison; no execution code |

## Blockers
- **Vault Markets data and specification** (backlog 7). Everything else proceeds without them. Provisional values are
  isolated in `config/brokers/PROVISIONAL_dev_xauusd.toml`, and any result produced with them is labelled `provisional_dev`.
