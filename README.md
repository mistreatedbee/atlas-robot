# Project Atlas — XAU/USD research foundation (Phase 1)

Phase 1 of a staged effort to build a multi-agent research system for **spot gold (XAU/USD) only**, and eventually a trading system
(scope: [docs/00_scope_xauusd.md](docs/00_scope_xauusd.md)).
This repository currently does **one thing**: ingest historical candles, validate them, and store them
reproducibly with provenance. It has **no trading capability**: no order code, no account access, no
credentials. A test enforces that boundary (`tests/test_config_and_safety.py`).

Start with [docs/00_scope_xauusd.md](docs/00_scope_xauusd.md), then [docs/01_research_report.md](docs/01_research_report.md).

| Document | Contents |
|---|---|
| [00 Scope: XAU/USD only](docs/00_scope_xauusd.md) | Authoritative scope, research questions, broker data checklist, operating principles |
| [01 Research report](docs/01_research_report.md) | Findings, what was built, evidence, the capital-target reality check |
| [02 Data sources](docs/02_data_sources.md) | Candidate gold price, reference and US macro sources compared |
| [03 Agents](docs/03_agents.md) | The 12 gold agents: responsibilities, permissions, dependencies, message contracts |
| [04 Architecture](docs/04_architecture.md) | System architecture, directory layout, storage decision, schema |
| [05 Data quality plan](docs/05_data_quality_plan.md) | Validation rules, policies, test mapping |
| [06 Indicator catalogue](docs/06_indicator_catalogue.md) | Candidate indicators: definitions, warm-up, tests, limits |
| [07 Research standards](docs/07_research_standards.md) | Hypothesis, backtest and governance rules |
| [08 Roadmap](docs/08_roadmap.md) | 8-phase plan, Phase 1 status, next milestone and its exact tests |
| [09 Decisions and risks](docs/09_decisions_and_risks.md) | Open decisions for you, technical risks |
| [10 Vault Markets data checklist](docs/10_vault_markets_data_checklist.md) | Step-by-step: what to get from support and MT5, file names, support message |
| [11 Status and backlog](docs/11_status_and_backlog.md) | Current state with evidence, prioritised backlog, blockers |
| [12 Decision log](docs/12_decision_log.md) | Engineering decisions, reasons, how to reverse |
| [13 Simulation rules](docs/13_simulation_rules.md) | Backtest rules v2 (fills, exits, costs, gate mapping, splits, metrics, baselines, leakage test) |

## Setup

Requires Python ≥ 3.11 (developed on 3.14.6, macOS). Runtime dependencies: the standard library plus
`certifi` (a CA bundle; python.org macOS builds cannot verify HTTPS without it). Dev dependency: `pytest`.

```bash
cd AshleyFx_Robot2.0
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                       # offline suite
ATLAS_LIVE_TESTS=1 .venv/bin/python -m pytest    # also checks the dormant reference connector against a real public API
```

No API keys are needed. The database is created on first use at `data/atlas.db` (SQLite); logs go to
`logs/atlas.jsonl` (JSON lines) and stderr.

## Usage

```bash
A=.venv/bin/atlas
$A instruments                                   # the registry: XAUUSD only

# Synthetic XAUUSD sample (clearly labelled; see data/sample/README.md)
$A ingest --provider sample_synthetic --instrument XAUUSD --timeframe M5 --start 2026-08-02T22:00Z --end 2026-08-28T21:00Z
$A validate --provider sample_synthetic --instrument XAUUSD --timeframe M5
$A show --provider sample_synthetic --instrument XAUUSD --timeframe M5 --limit 5

# Broker export (Phase 2): place it at data/imports/XAUUSD_M1.csv and set source_timezone in atlas.toml
$A ingest --provider user_csv --instrument XAUUSD --timeframe M1 --start 2025-01-01
$A runs                                          # ingestion run history, checkpoints, errors

# Research (SYNTHETIC sample + PROVISIONAL costs = software validation only)
$A backtest --experiment experiments/baseline_session_long_sample.toml --period validation [--cost-multiplier 2]
$A random-baseline --experiment experiments/baseline_session_long_sample.toml --period validation --seeds 1000 --stop-usd 25
$A experiments                                   # the append-only experiment ledger
$A risk-feasibility --spec config/brokers/PROVISIONAL_dev_xauusd.toml --equity 500 --usd-to-account 16.6 \
    --spread-usd 0.30 --slippage-usd 0.10 --source "provisional"
$A health                                        # exit 0 ok / 1 warn / 2 critical
```

Times on the command line are UTC. `--end` is exclusive and is capped at the last fully closed bar.
If an ingestion fails or the process is killed, **rerun the identical command**: it resumes from the
last committed chunk.

The registry is configuration (`config/instruments.toml`). The project scope (`[scope]` in `atlas.toml`) authorises
XAUUSD only, and loading fails if any other instrument is enabled. To import a broker export, place it at
`data/imports/XAUUSD_{TF}.csv` and set the export's timezone under `[providers.user_csv]` in `atlas.toml`.

## Layout

```
atlas.toml                 settings + provider definitions (no secrets)
config/instruments.toml    instrument registry
atlas/                     package: models, sessions, validation, storage, ingest, http, providers/, cli
scripts/                   generate_sample_data.py
data/sample/               SYNTHETIC XAUUSD sample dataset + its README
data/imports/              user-supplied files (git-ignored)
tests/                     pytest suite (fixtures/ holds a recorded response used by the dormant reference connector)
docs/                      Phase 1 deliverables
```
