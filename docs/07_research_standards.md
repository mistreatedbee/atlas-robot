# 07 — Research standards and governance (XAU/USD)

## Before testing
1. **Pre-register** the hypothesis (schema in doc 03): rules, universe, timeframe, cost model, baseline, data periods,
   parameter ranges, maximum number of trials, and success criteria. Registration stores a SHA-256 of the spec, and it can
   only be superseded, never edited.
2. **Split chronologically:** train → validation → test, with an embargo of at least the longest holding period plus
   the indicator warm-up between segments. The **test set is touched once per hypothesis**, and every access is logged.
3. **Declare** the gap policy, fill model, latency and cost model versions.

## During testing
- **Costs are mandatory:** spread from the broker's bid/ask data (or its measured spread distribution, including the
  widening after the daily reopen and around US releases), commission (raw-spread accounts), slippage (at least one tick
  for market orders, more during news), swap for positions held through the daily break, and ZAR↔USD conversion of
  account P&L. Every result is also reported at **1.5× and 2× costs**.
- **Broker constraints:** respect minimum volume, volume step, stops level and freeze level. Trades the broker would have
  refused are not counted.
- **Fills:**
  - Market orders fill at the next bar's open plus slippage, never at the signal bar's close.
  - Limit orders fill only if price trades *through* the limit, and fills are not assumed at touch.
  - If stop and target are both inside one bar, assume the stop hit first unless tick data says otherwise.
- **Latency:** signal at bar close; earliest action at the next bar plus the configured delay.
- **Walk-forward** when parameters are fitted: rolling re-fit, with out-of-sample segments concatenated for evaluation.
- **Sensitivity:** report performance across the parameter neighbourhood. A narrow profitable peak counts as evidence
  *against* the hypothesis.
- **Regimes:** break results down by volatility regime, session and trend/range state.
- **Multiple testing:** count every variant tried and record it in the ledger. Use the deflated Sharpe ratio or White's
  Reality Check / SPA for the selected strategy, and a Bonferroni or Holm correction for families of simple tests.
- **Baselines:** compare against random entry with the same holding time, session and exposure, and against "no trade".
  A strategy that does not beat random entry *after costs* is rejected. The multi-agent system (Phase 6) must beat the
  best single simple strategy out-of-sample, or the simpler system is kept.
- **Events:** report results inside and outside windows around tier-1 US releases separately.

## Metrics (all net of costs)
Net return, expectancy per trade (in R and in currency), profit factor, max drawdown (depth and duration), win rate,
average win / average loss, trade count, exposure, Sharpe/Sortino (annualised for gold's ~23×5 schedule),
Calmar, tail metrics (worst day, worst 10 trades), and cost as a percentage of gross P&L.

**Interpretation rules:**
- A high win rate means nothing without the loss distribution and costs. Scalping strategies often show 70%+ win rates and
  negative expectancy.
- A strategy with fewer than about 200 out-of-sample trades is inconclusive at best.
- **Never rank by highest historical profit.** Rank by out-of-sample, cost-adjusted, robustness-checked evidence.

## Governance: findings and decisions
- Every finding uses the `Finding` contract: conclusion, observations, data source and timestamps, confidence or
  uncertainty, limitations and reproducibility info.
- Facts, hypotheses, estimates and recommendations are separate `kind`s and are never merged into one claim.
- The coordinator **links and queues** findings for review. It does not edit, average or suppress them. Contradictory
  findings are both kept and flagged for a human decision.
- Failed and losing experiments are kept permanently. The ledger is append-only.
- Changes to risk limits, strategy promotion (research → paper → live) and enabling execution require **explicit
  human approval**, which is recorded with the approver and timestamp.
- Any trade proposal passes the deterministic risk gate. Agents cannot bypass it, and LLM text never reaches it.
- All XAUUSD positions are one combined exposure. No martingale, no averaging-down grids, no loss-recovery sizing.
  The capital target is never a risk input.
- Self-modifying code is not permitted. Code changes go through version control and review like any other change.
