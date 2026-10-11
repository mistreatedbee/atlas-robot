# 08 — Staged roadmap (XAU/USD) and acceptance criteria

No phase starts until the previous phase's criteria are met **and you have reviewed them**.

| Phase | Goal | Status |
|---|---|---|
| 1 | Architecture and research documentation for XAU/USD only; data foundation | **Complete pending your review** (criteria below) |
| 2 | Gold historical data pipeline on the broker's real data | Next. Blocked on D1 (broker) |
| 3 | Indicator, structure, session, volatility/regime and macro research modules | — |
| 4 | Pre-registered gold scalping hypotheses plus a reproducible backtesting framework | — |
| 5 | Out-of-sample validation with realistic costs | — |
| 6 | Agent integration; does collaboration beat a simple baseline? | — |
| 7 | Demo trading; risk gate, monitoring, recovery, duplicate-order prevention | — |
| 8 | Live execution, only after readiness checks **and** your explicit authorisation | — |

## Phase 1 acceptance criteria (status as of 2026-10-11)
- [x] Scope is documented (doc 00) and enforced: only XAUUSD is enabled, and loading fails otherwise.
- [x] The architecture and the 12 gold agents' responsibilities, permissions and contracts are documented (docs 03, 04).
- [x] Candidate gold data sources and their limitations are documented (doc 02).
- [x] The repository sets up from the README instructions (verified on this machine with Python 3.14.6).
- [x] Ingestion and validation work on the declared source: the synthetic XAUUSD sample, with exact expected defects.
- [x] Data is stored and retrieved reproducibly (repeat validation gives an identical summary; idempotent reruns).
- [x] Automated tests cover the core validation rules (102 offline tests pass).
- [x] Errors are logged; recoverable ingestion was demonstrated (simulated in tests; real `SIGKILL` on live data earlier).
- [x] No trading capability exists (safety tests).
- [ ] **Your review** of docs 00–09 and the open decisions in doc 09.

## Phase 2 — next smallest verifiable milestone: "Broker XAUUSD M1 import"

**Prerequisite from you:** choose the broker (D1). Open a **demo** account; no live credentials are needed. Export from MT5:
- the XAUUSD symbol specification (screenshot or copied text);
- M1 history for as far back as the server provides, via MT5 → View → Symbols → Bars → Export;
- one week of ticks via Symbols → Ticks → Export.

**Scope of the code change (small):**
1. `brokers/` — a versioned XAUUSD spec file (TOML) and a loader that validates it.
2. An MT5 export parser for MT5's tab-separated `<DATE> <TIME> <OPEN> … <TICKVOL> <VOL> <SPREAD>` format, as a variant of the CSV provider.
   The spread column is stored as reported. Its exact meaning (bar minimum vs other) is **verified against the tick export
   before it is used as a cost**.
3. Server-time-zone inference from the weekend gap and daily-break positions. Import is refused if the declared zone disagrees.
4. Reconciliation of observed gaps against `xau_cfd`, producing a broker-hours findings report.

**Exact tests that establish completion:**

| Test | Proves |
|---|---|
| `test_broker_spec_valid_and_rejects_inconsistent` | Required fields present; `volume_min ≥ volume_step > 0`; `tick_size == 10^-digits`; contract size > 0 |
| `test_mt5_bar_export_parses` | A fixture in MT5 format (recorded from the real export) parses with correct UTC conversion, tick volume as `tick_count` and spread retained |
| `test_mt5_malformed_rows_rejected` | Bad rows go to `rejected_records`, not candles |
| `test_infer_server_offset_winter_and_summer` | Bars shifted +2 h (winter) and +3 h (summer) give the right offsets per DST period |
| `test_import_refused_on_timezone_mismatch` | A declared UTC+0 against an inferred UTC+2 raises before any write |
| `test_calendar_reconciliation_reports_broker_differences` | A broker with a 1 h 05 m daily break gives a finding, not silent acceptance |
| `test_registry_symbol_matches_broker_spec` | The registry's `user_csv` symbol equals the broker spec symbol |
| Real-data acceptance (manual, recorded in a quality report) | ≥ 12 months of broker XAUUSD M1 ingested; invalid = 0 or each explained; every unexpected gap classified (holiday / broker outage / unknown); identical rerun gives 0 inserted; spec stored with file SHA-256 |

## Later phases — acceptance criteria (summary)
- **Phase 2 (remainder):**
  - tick import (Parquet) with tick → M1 reconciliation;
  - spread profile by hour and around releases;
  - a second broker's demo data for comparison.
- **Phase 3:**
  - every feature in doc 06 has reference-value and look-ahead truncation tests;
  - session/regime labels are causal and versioned;
  - FRED/ALFRED point-in-time series and a release calendar, with a test that no event is visible before its release time.
- **Phase 4:**
  - at least four pre-registered hypotheses (momentum, pullback, range, breakout) with no-trade conditions;
  - the backtester reproduces hand-computed trades on a fixture;
  - conservative intrabar ordering; costs from versioned cost models; broker constraints enforced.
- **Phase 5:**
  - walk-forward plus a single-use test set;
  - results at 1×, 1.5× and 2× costs and with added delay; multiple-testing-adjusted significance; baseline comparison;
  - every experiment in the ledger, including failures. "No strategy passes" is an acceptable, reportable outcome.
- **Phase 6:** the combined agent system beats the best simple strategy out-of-sample after costs, or it is not adopted.
- **Phase 7:**
  - at least 3 months of demo trading;
  - the risk gate rejects every limit breach in tests;
  - restart without duplicate orders (reconciliation tested);
  - trading stops on stale feed or unverifiable account state;
  - demo results within the predicted cost/slippage bounds.
- **Phase 8:** a written readiness review, a small capital limit, and your explicit sign-off.
