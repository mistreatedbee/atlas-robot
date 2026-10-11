# 09 — Unresolved decisions and technical risks

## Decisions needed from you
| ID | Decision | Why it matters | My recommendation |
|---|---|---|---|
| **D1** | **Which broker** (and server) for XAUUSD | Feed, spread, hours, contract spec, server time and execution rules are all broker-specific, and nothing about costs can be concluded without them | An FSCA-regulated broker offering MT5, XAUUSD with published specs, tick-history export, and a demo account. Choose before Phase 2. |
| **D2** | Account type: standard / raw-spread + commission / **cent** | At R500, a 0.01-lot minimum (often 1 oz) forces 3–17% risk per trade at realistic stops (doc 01 §5) | Evaluate cent accounts or smaller volume steps, or a larger balance, before any demo-to-live step |
| D3 | History target for research | Out-of-sample testing needs years and several regimes | ≥ 5 years of M1 if the broker provides it; otherwise supplement with Dukascopy, labelled as a different venue (after checking its terms) |
| D4 | Paid economic-calendar consensus data | Needed only for "surprise" hypotheses | Defer; use official release times first |
| D5 | Tick storage | Ticks are about 100× the size of M1 bars | Partitioned Parquet (adds the `pyarrow` dependency) |
| D6 | Indicator reference | TA-Lib needs a C library | Own NumPy implementation, tested against stored TA-Lib reference values |
| D7 | Version control | Reproducibility records need a commit hash | `git init` now (I have not done this without your go-ahead) |
| D8 | The dormant Binance code | Out of scope but tested | Keep as the reference provider implementation, or delete on request |
| D9 | 24/7 hosting (Phase 7) | Continuous monitoring needs an always-on machine or VPS near the broker server | Decide in Phase 6 |

## Technical and research risks
| Risk | Impact | Mitigation |
|---|---|---|
| Spread consumes the M1/M5 edge | Scalping infeasible | Measure the cost-to-range ratio first (research question 2); "do not scalp" is an acceptable conclusion |
| Broker feed differs from other sources | Results don't transfer | Research on the execution broker's own data; cross-broker test (question 8) |
| Wrong server time zone or DST | Every session and event study is wrong | Inference plus refusal on mismatch (Phase 2 tests) |
| MT5 history is shallow or has gaps | Too little out-of-sample data | Gap reports; second source labelled separately |
| Bar spread field misinterpreted | Costs understated | Verify against the tick export before use |
| Overfitting via many hypotheses | False discoveries | Pre-registration, trial budgets, deflated Sharpe/SPA, single-use test set |
| Point-in-time leakage from revised macro data | Inflated backtests | ALFRED vintages, release-time gating tests |
| Minimum lot vs small account | Excessive risk per trade | D2; the risk gate refuses trades above the risk cap even if that means never trading |
| Pressure to meet R100,000 by 31 Dec 2026 | Reckless risk | Target is excluded from risk inputs (doc 00); progress reported honestly |
| Broker execution (requotes, slippage at news, stop-outs) | Live underperforms backtest | Demo phase with drift monitoring; event no-trade windows |
| Single SQLite writer | Contention once collectors run continuously | Move to Postgres/Timescale at Phase 7 if needed |
| Local machine only, no backups | Data loss | Backups and snapshots before Phase 4 ledger work |
