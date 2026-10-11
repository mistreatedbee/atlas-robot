# 03 — Gold agent architecture (XAU/USD only)

## Principles
- **Deterministic code by default.** Data, indicators, sessions, regimes, backtests and risk are tested modules.
  Language models are reserved for summarising news, drafting hypotheses and reviewing results. An "agent" is a
  component with a defined responsibility, not necessarily an LLM.
- **Structured messages only** (contracts at the end). Free text lives only in `narrative` fields, which are never parsed
  into actions.
- **Every message carries `instrument_id`, and it must be `XAUUSD`.** The coordinator rejects anything else.
- **Shared definitions:**
  - All agents use the same UTC timestamps, the same `xau_cfd` calendar version, and the same candle series identified by
    `(provider, venue, timeframe)` and a quality-report ID.
  - Prices and distances are in **US$ per ounce**, never "pips".
- **No agent can trade or override risk.** Only a typed `TradeProposal` reaches the deterministic risk gate (Phase 7+).
- **Disagreement is preserved.** Conflicting findings are kept side by side.

## Responsibility and dependency matrix

| # | Agent | Implementation | Consumes | Produces | Built in phase |
|---|---|---|---|---|---|
| 1 | Gold Research Coordinator | deterministic workflow, plus optional LLM summarising | all findings | tasks, review queue, roadmap status | 6 (rules from 3) |
| 2 | XAU/USD Historical Data Specialist | **deterministic** (exists) | broker exports/APIs, registry | candles, ticks, quality reports, broker spec records | **1 (built)** → 2 |
| 3 | Price Action & Technical Analysis | deterministic feature library; LLM optional | 2 | versioned features, findings | 3 |
| 4 | Volatility & Regime | deterministic | 2, 7, 6 | causal regime labels, "unsuitable market" flags | 3 |
| 5 | Liquidity & Execution | deterministic | 2 (ticks, bid/ask) | spread/slippage cost models | 2–3 |
| 6 | US Macro & Gold Fundamentals | deterministic collectors; LLM for classification | FRED/ALFRED, BLS/BEA/Fed calendars, news | point-in-time events and series | 3 |
| 7 | Trading Session | deterministic | 2, broker hours | session/overlap/holiday calendars, session statistics | 2–3 |
| 8 | Strategy Research | LLM-assisted drafting; deterministic spec validation | 3–7 | pre-registered `Hypothesis` records | 4 |
| 9 | Backtesting & Statistical Validation | **deterministic** | 2, 3, 5, 8 | `ExperimentResult` | 4–5 |
| 10 | Risk & Portfolio Protection | deterministic policy research; deterministic **risk gate** | 9, broker spec | `RiskPolicy` proposals; gate (7) | 5 → 7 |
| 11 | Learning & Performance | deterministic metrics; LLM narrative | 9, demo/live fills | degradation alerts, change proposals | 5–7 |
| 12 | Reliability & Security | ops tooling | all | health, alerts, audit, kill switch | 1 (logging) → 7 |

Order: **2 → 7 → (3, 4, 5, 6) → 8 → 9 → (10, 11)**. 1 reviews everything; 12 underpins everything.

## Agent specifications

Each entry lists:
- **Must NOT** — permissions it must never hold.
- **Tests** — what the automated suite must prove.

### 1. Gold Research Coordinator
- **Responsibilities:**
  - maintain the roadmap and the phase gates (doc 08);
  - assign tasks;
  - verify that every finding is about XAUUSD and uses compatible data IDs, calendar versions and units;
  - route findings to review.
- **Inputs / outputs:** findings, results and task status in; tasks and the review queue out.
- **Validations:**
  - schema check on every message;
  - `instrument_id == "XAUUSD"`;
  - the referenced `quality_report_id` exists and its status is not `invalid`;
  - consistent units.
- **Failure modes:** manufacturing consensus; dropping findings. Mitigated because the coordinator only links findings
  and never edits them.
- **Must NOT:** change risk limits, approve its own proposals, modify code or delete records.
- **Tests:** a non-gold finding is rejected; a finding with a stale or invalid data reference is rejected; contradictory
  findings are both retained.

### 2. XAU/USD Historical Data Specialist (built; extended in Phase 2)
- **Responsibilities:**
  - acquire and normalise broker M1/M5 bars and ticks (bid/ask) into UTC;
  - validate them;
  - record provenance and the broker symbol specification;
  - document differences between brokers.
- **Inputs / outputs:**
  - in: MT5 exports, broker APIs;
  - out: `candles`, ticks (Phase 2), `ingestion_runs`, `quality_reports`, `candle_conflicts`, `rejected_records`, `broker_specs`.
- **Validations:**
  - the rules in doc 05;
  - in Phase 2 also: server-time-zone inference from the weekend-gap position, tick → M1 re-aggregation against
    exported M1, and spread ≥ 0.
- **Failure modes:** wrong server time zone; truncated history; broker revisions; silent symbol changes.
- **Must NOT:** fill gaps, overwrite history, hold trading credentials, or ingest any instrument other than XAUUSD.
- **Tests:** 102 existing tests, plus the Phase 2 tests in doc 08.

### 3. Gold Price Action & Technical Analysis Specialist
- **Responsibilities:**
  - market structure (swing highs/lows, higher highs/lower lows, structure breaks), support/resistance zones, breakouts,
    pullbacks and failed breakouts, candlestick patterns;
  - the indicator catalogue (doc 06);
  - multi-timeframe alignment (M1/M5 against M15/H1/H4).
- **Key duty:** measure **incremental, independent information**: correlation and redundancy between features, and the
  out-of-sample information gain over price-only baselines. Indicators that add nothing are dropped.
- **Validations:** reference-value tests; look-ahead truncation tests; higher-timeframe features use only the last
  *closed* higher-timeframe bar.
- **Must NOT:** emit trade signals directly; use bars beyond the decision time.

### 4. Gold Volatility & Market Regime Specialist
- **Responsibilities:**
  - causal regime labels: trending, ranging, expanding volatility, contracting volatility, abnormal (spread blow-out,
    gap, news shock, stale feed);
  - session- and event-conditioned volatility profiles;
  - an explicit **"scalping disabled"** state with measurable triggers.
- **Validations:** labels at *t* use data ≤ *t*; definitions are versioned; stable under re-computation.
- **Must NOT:** relabel history after seeing strategy results.

### 5. Gold Liquidity & Execution Specialist
- **Responsibilities:**
  - spread distribution by hour, session, day-of-week and minutes-from-release;
  - tick-activity profiles (labelled *broker tick volume*, a proxy);
  - slippage and latency assumptions;
  - stops level, freeze level and minimum-volume constraints;
  - cost-to-range ratio as a feasibility metric for M1/M5 scalping.
- **Outputs:** versioned `CostModel` records used by every backtest.
- **Must NOT:** treat tick volume as traded volume; use average spread where tail spread governs (open, news).

### 6. US Macro & Gold Fundamentals Specialist
- **Responsibilities:**
  - Fed decisions and expectations; CPI, PCE, NFP and claims;
  - nominal and real 10-year yields; the dollar index;
  - central-bank gold demand (WGC); geopolitical events.
- Each record carries the source, the official release time, the first-seen time, and the vintage. Facts are stored
  separately from interpretations.
- **Validations:**
  - a backtest can only see events with `available_at ≤ decision time`;
  - revised values are new vintages, not overwrites;
  - LLM summaries must cite source text and are never used as facts.
- **Must NOT:** emit trade instructions; scrape in breach of terms.

### 7. Gold Trading Session Specialist
- **Responsibilities:**
  - Asia / London / New York sessions and overlaps; London AM/PM auction times; 08:30 New York releases;
  - the broker's daily break and Sunday open, DST transitions (US and UK switch on different dates), holidays and early closes;
  - session-conditioned statistics *after costs*.
- **Validations:**
  - the calendar (`xau_cfd`, versioned) is reconciled against observed gaps in broker data, with mismatches reported;
  - DST tests for both US and UK changeovers.
- **Must NOT:** assume a universal session edge. Each session effect is a hypothesis for agent 8.

### 8. Gold Strategy Research Specialist
- **Responsibilities:**
  - pre-register explicit, falsifiable hypotheses, starting with four separate families: momentum, trend pullback, range
    reversion, breakout;
  - each spec states: entry, exit, stop placement (US$ or ATR multiple), invalidation, expected cost per trade from the
    current `CostModel`, regimes/sessions/events in which it must **not** trade, baseline, data periods and trial budget.
- **Must NOT:** test on held-out data; edit registered hypotheses (only supersede them).

### 9. Gold Backtesting & Statistical Validation Specialist
- **Responsibilities:** event-driven M1/M5 backtests using broker bid/ask, spread from the cost model, commission, slippage,
  latency, stops-level rules and conservative intrabar ordering; chronological train/validation/test; walk-forward;
  sensitivity to parameters, **costs (×1.5, ×2)** and delay; multiple-testing correction; baselines.
- **Rejects** strategies that pass only in narrow parameter pockets or under optimistic execution.
- **Must NOT:** delete failed experiments; touch execution; use `synthetic_sample` data for results.

### 10. Gold Risk & Portfolio Protection Specialist
- **Responsibilities:**
  - treat all XAUUSD positions as **one combined exposure**; block duplicate or correlated entries (same direction within
    N minutes or a price band count as one idea);
  - position sizing from stop distance and minimum volume step;
  - maximum open risk, daily loss limit, maximum drawdown, margin checks, emergency flatten/shutdown;
  - **no risk increase after losses**: no martingale, no averaging-down grids.
- **Gate (Phase 7):** a deterministic module outside any agent's control. It rejects proposals that breach limits, arrive
  with stale prices or unverifiable account state, or fall in a disabled regime or event window.
- **Must NOT:** take the capital target as an input; be modifiable by any agent.

### 11. Gold Learning & Performance Specialist
- **Responsibilities:** append-only experiment and trade ledger; per-regime and per-session attribution; live/demo-vs-backtest
  drift detection; proposals as new hypotheses compared against the **current approved baseline**.
- **Must NOT:** modify execution code, disable risk controls or promote strategies automatically.

### 12. Reliability & Security Specialist
- **Built:** structured logs, resumable runs, crash detection, host allowlist, credential-free config, safety tests.
- **Later:**
  - supervision and heartbeats; feed-staleness detection;
  - **duplicate-order prevention** (idempotent client order IDs, reconciliation against broker positions on restart);
  - secrets in the OS keychain; trade-only credentials with withdrawals disabled;
  - alerting; a kill switch;
  - "stop initiating trades if prices are stale, data unreliable or account state unverifiable".

## Message contracts (JSON, validated; `instrument_id` must be `"XAUUSD"`)

```jsonc
{ "type": "finding", "schema_version": 1, "finding_id": "uuid", "agent": "session",
  "instrument_id": "XAUUSD", "created_at_utc": "…",
  "kind": "fact | hypothesis | estimate | recommendation",
  "conclusion": "short, falsifiable statement",
  "observations": [{"metric": "median_spread_usd", "value": 0.0, "unit": "USD/oz", "n": 0, "condition": "London open"}],
  "data": [{"provider": "user_csv", "venue": "<broker>", "timeframe": "M1", "start_utc": "…", "end_utc": "…",
            "data_label": "user_file", "quality_report_id": "uuid", "calendar_version": "xau_cfd@1"}],
  "confidence": {"method": "bootstrap CI | p-value | qualitative", "value": "…"},
  "limitations": ["…"], "reproducibility": {"code_version": "…", "config_sha256": "…", "command": "…"},
  "narrative": "free text, never parsed" }

{ "type": "hypothesis", "hypothesis_id": "uuid", "supersedes": null, "instrument_id": "XAUUSD",
  "family": "momentum | pullback | range | breakout", "execution_tf": "M1", "context_tf": ["M15", "H1"],
  "entry_rules": [], "exit_rules": [], "stop": {"type": "atr_multiple", "value": 1.5}, "invalidation": [],
  "no_trade_conditions": ["regime=abnormal", "within 10 min of tier-1 US release", "spread > p90"],
  "cost_model_id": "uuid", "baseline": "random entry, same holding time and exposure",
  "periods": {"train": [], "validation": [], "test": []}, "max_trials": 50,
  "success_criteria": {"min_oos_trades": 200, "net_expectancy_usd_gt": 0, "pf_gt": 1.1, "survives_cost_x1_5": true} }

{ "type": "trade_proposal", "proposal_id": "uuid", "instrument_id": "XAUUSD", "strategy_version": "…",
  "side": "buy|sell", "entry": {"type": "market|limit", "price": 0.0}, "stop_loss": 0.0, "take_profit": 0.0,
  "volume_lots": 0.01, "risk_usd": 0.0, "price_timestamp_utc": "…", "valid_until_utc": "…",
  "evidence": {"experiment_id": "uuid"} }
// Gate reply: {"proposal_id": "…", "decision": "reject|approve", "checks": [{"rule": "max_open_risk", "passed": false}]}
```
