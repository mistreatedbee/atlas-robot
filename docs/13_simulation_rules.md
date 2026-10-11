# 13 — Backtest simulation rules (XAU/USD, M1/M5) — v2.1

*Author: quantitative researcher role. v1 was challenged by the independent reviewer (15 findings). This v2 adopts
them as recorded in the table at the end. Where a rule could go either way, it leans toward overstating costs and
losses.*

## R1 Data
1. **Input:** one stored candle series (XAUUSD, one provider/venue, M1 or M5), excluding `invalid` candles. Bars must
   be **bid** OHLC; other price bases are rejected (v2.1). Missing bars are never synthesised. Warm-up bars do not
   enter results: equity curve, daily returns and exposure count only bars in the period.
2. **Results inherit the data label.** `synthetic_sample` results are software tests only, and the ledger says so.
3. **Spread used at bar t:**
   - `spread(t) = max(model spread at t, bar spread_points × tick_size)`. The bar value is used only when the source reports it.
   - **Model spread** = `base_spread_usd`, raised to `reopen_spread_usd` for the first `reopen_minutes` after the
     daily or weekly reopen (18:00 New York).
   - When `ask_*` columns exist, ask = max(bar ask, bid + spread(t)). Otherwise ask = bid + spread(t).
   - The spread is constant within a bar. This is a stated optimism; tick-based costs come later.

   All spread and slippage values stay **provisional** until measured from Vault tick data.

## R2 Decision timing and order eligibility
1. A strategy decides at the **close of bar t**, using only bars 0..t.
2. **Earliest fill:** the open of bar index t+1+L, where L is the latency in bars (default 0, recorded on every run).
   A sensitivity run at L=1 is supported.
3. **Fill rule:** the order fills at the first existing bar whose open lies within `max_fill_wait`
   (default 5 min) after that earliest time.
   - **Expires** (`expired_wait`) if no bar opens within the wait: MT5 omits minutes with no ticks, so a short wait
     is realistic. Expiry counts are reported by UTC hour.
   - **Cancelled** (`expired_closure`) if a scheduled closure lies between the decision and the fill.
4. **No-entry windows.** No entry fills:
   - within `pre_close_buffer` (default 5 min) before a scheduled closure;
   - within `reopen_minutes` after a reopen.

   These are recorded as `blocked_window`.
5. **One combined exposure:**
   - At most one position or pending order at a time.
   - Signals that arrive while one exists are recorded as `ignored_in_position` / `ignored_pending`.
   - A position closed on bar t frees the slot for a decision at the close of bar t.

## R3 Entries and the risk gate
1. Market orders only.
   - Long fills at ask_open + slippage.
   - Short fills at bid_open − slippage.
2. **Stop and take-profit:** given as distances in US$/oz from the fill price, then rounded to `tick_size`.
   - The stop is rounded **away from** entry.
   - The take-profit is rounded **toward** entry.
   - A stop distance ≤ 0 is invalid. The take-profit is optional.
3. Every entry passes `atlas.risk.gate.evaluate` immediately before filling, with this exact mapping:
   - `equity` = realised balance (always flat at entry);
   - `free_margin` = equity;
   - `open_risk` = 0;
   - `day_start_equity` = mark-to-market equity at the first bar of the trading day;
   - `peak_equity` = highest mark-to-market equity at any bar close so far;
   - `trades_today` = filled entries this trading day;
   - `verified_at_ms` = `price_time_ms` = `now` = fill-bar open;
   - `CostInputs.spread_usd` = spread(fill bar); slippage = the configured slippage.

   The gate's lot size is used. A rejection is recorded with the failed rule names and no position opens.
4. **Slippage** must be at least 1 tick (the config is validated). News-window widening needs an event calendar
   and is not yet modelled (limitation).

## R4 Exits (within a bar: price exits first, then time exits at the close)
1. **Bar opens beyond a level** (bar after entry only):
   - **Long:** if bid_open ≤ stop, exit at bid_open − slippage (`stop_gap`). If bid_open ≥ tp, exit at tp (`tp_gap`, no extra gain).
   - **Short:** symmetric on ask_open.
2. **Intrabar, including the entry bar after its open:**
   - Long: the stop is hit if bid_low ≤ stop; the take-profit if bid_high ≥ tp + 1 tick.
   - Short: the stop is hit if ask_high ≥ stop; the take-profit if ask_low ≤ tp − 1 tick.
   - **If both are hit, the stop came first.**
   - Stops fill at the stop price minus slippage (long) or plus slippage (short). Take-profits fill exactly at tp.
3. **Then at the bar close, in this order:**
   1. Max-hold reached: exit at the close.
   2. **Scheduled closure** within `pre_close_buffer` of this bar's close (from the `xau_cfd` calendar only): exit
      at the close.
   3. Last bar of the data: exit at the close (`end_of_data`).

   Time exits fill at bid_close − slippage (long) or ask_close + slippage (short).
4. **Unexpected data gaps are held**, never anticipated. The first bar after the gap applies R4.1, and the trade is
   flagged `held_through_gap`.
5. Losses beyond the planned risk (gap fills) are recorded as they occur. R-multiples below −1 are never capped.
   - **R unit (v2.1)** = (|entry − stop| + slippage) × oz × lots × FX + round-turn commission. A clean stop-out is
     exactly −1R.
   - The gate's planned risk, which also counts the spread, is reported separately as `planned_risk`.
6. Holding through scheduled closures is not supported in v1. Swap costs are unverified, so session exits are
   mandatory.
7. **Missed session exit (v2.1).** If no bar closed inside the pre-close buffer, for example because the quiet
   minutes before 17:00 New York are missing, the position is closed at the **open of the first bar after the
   closure**: bid_open − slippage for a long, ask_open + slippage for a short. The reason is `held_through_closure`.
   The gap risk is borne, never avoided.
8. **Entry bar (v2.1).** R4.1 also applies on the entry bar. A stop inside the spread is hit at that bar's open and
   is never filled better than the market.

## R5 Costs and currency
1. Spread is implicit in bid/ask fills. Slippage applies to market, stop and time exits.
2. Commission comes from the spec, per lot round turn; half is charged at entry and half at exit.
3. Results are computed in USD and converted to the account currency at a **constant** configured rate.
   - This is an assumption. Lot sizing over long spans would really vary with USD/ZAR.
   - A daily FX series is on the backlog.
   - Both USD and account-currency P&L are reported.
4. Stress runs scale spread and slippage by ×1.5 and ×2.

## R6 Account
1. **Starting equity:** from configuration.
2. **Drawdown:** uses mark-to-market equity at each bar close, valued at the exit-side price.
3. **Trading day:** labelled by the New York date of (bar open + 7 h), so the day boundary is 17:00 New York with
   DST handled. Day-start equity and trades-per-day reset at that boundary.
4. **In-simulation kill switch:** may be engaged by configuration.
5. **Stopping:** the run stops if equity ≤ 0.

## R7 Data splits and test-set discipline
1. Train, validation and test periods must be chronological and must not overlap.
   - **Embargo** ≥ max(1 day, (max_hold_bars + longest indicator lookback) × timeframe).
2. Bars before a period may be used for indicator warm-up only. No orders are decided or filled before the period start.
3. A **test-period** run requires an explicit flag and a `hypothesis_id`.
4. **Reserving the test window (v2.1).** The test window is reserved per data series before any run of the config.
   - A window cannot be reserved on data that an earlier non-test run already used.
   - Once reserved, no train or validation run may overlap it.
5. **One look per hypothesis (v2.1).** A hypothesis gets one *look* at the test set. A look is its configuration
   without the cost multiplier, so the ×1.5 and ×2 stress runs belong to the same look.
   - A different configuration, or an exact repeat, is refused unless overridden, and the override is recorded.
6. **Prior looks are recorded (v2.1).** Every test run records how many prior looks the window has had, for
   multiple-testing adjustment.

## R8 Metrics (net of all costs)
- **Trade results:** net P&L (account currency and USD), return %, trade count, win rate (win = net > 0),
  average win, average loss, profit factor (reported as "undefined" with zero losses), expectancy (currency and R).
- **Costs:** total costs and costs per trade in R.
- **Drawdown:** max depth (currency and %) and duration in trading days, with an "unrecovered" flag.
- **Exposure:** fraction of bars in a position.
- **Signals not traded:** counts by reason, plus expired orders by UTC hour.
- **Sharpe / Sortino:**
  - computed from daily returns over **all** trading days in the period, with flat days counted as 0;
  - daily return = day P&L / day-start equity; annualised with √252;
  - reported only with ≥ 60 trading days, together with an approximate 95% confidence interval
    (standard error ≈ √((1 + ½S²)/n)).

## R9 Baselines
1. **No-trade:** the benchmark every strategy must beat after costs.
2. **Matched random entry:**
   - Signal probability per UTC-hour bucket = the strategy's **signals** ÷ the decision bars in that bucket (v2.1).
     Matching on signals rather than fills lets the random entries pass through the same gate and window
     filtering as the strategy did.
   - It copies the strategy's own stop and take-profit, plus its max-hold, gate and window rules, with random side.
   - The summary is recorded in the ledger.
   - Run over N seeds (≥ 1,000 for conclusions; fewer only in software tests); report the strategy's percentile.
3. **Session-long:**
   - Enters long once per trading day at the first decision after the reopen window.
   - It has a configured catastrophic stop, because the gate requires a stop, and exits by the session rule.
   - It measures passive drift plus costs.

## R10 Leakage test (required for every strategy)
For ≥ 20 cut points T (including cuts adjacent to closures):
- every decision record on a bar before T must match the full-data run exactly: time, side, fill time, price,
  stop, take-profit, lots, and every rejected, ignored or expired record;
- trades still open at T are excluded from the comparison;
- a second comparison replaces all bars after T with random prices and must also match.

## Review disposition (v1 → v2)
| # | Finding | Disposition |
|---|---|---|
| 1 | Exiting before an unexpected gap used future knowledge | Adopted (R4.3, R4.4) |
| 2 | Constant spread is optimistic at the reopen; no entry window before closure | Adopted (R1.3, R2.4). Measured p90 profile once Vault ticks arrive |
| 3 | Embargo too short; test-access check bypassable by changing params | Adopted (R7.1, R7.3) |
| 4 | Gate inputs ambiguous | Adopted (R3.3) |
| 5 | Session-long baseline has no stop and enters on the reopen bar | Adopted (R9.3) |
| 6 | Random baseline not matched | Adopted (R9.2) |
| 7 | Leakage test underspecified | Adopted (R10) |
| 8 | Metric definitions ambiguous | Adopted (R8) |
| 9 | Zero latency, slippage unstated | Adopted: slippage ≥ 1 tick, L recorded, L=1 sensitivity. News widening: limitation |
| 10 | Gap through the take-profit | Adopted (R4.1) |
| 11 | Exit order within a bar | Adopted (R4 header, R2.5) |
| 12 | Tick rounding | Adopted (R3.2) |
| 13 | Missing quiet M1 bars expire orders | Adopted with change: fill at the next bar within `max_fill_wait`; expiries reported by hour |
| 14 | Constant FX rate | Partially: constant rate kept and labelled; USD P&L also reported; daily FX series on the backlog |
| 15 | Trading-day label | Adopted (R6.3) |

## Review disposition (v2 → v2.1, implementation review of commit 0937580)
| # | Finding | Fix |
|---|---|---|
| 1 | Position held through a closure when the pre-close bars were missing | R4.7 `held_through_closure` exit at the first open after the closure |
| 2 | The safety scan had been weakened | Word patterns restored; only SQL trigger clauses exempted; behavioural GET-only test added |
| 3 | Price basis not checked | Engine rejects non-bid bars |
| 4 | Ledger child rows could be inserted or edited; status fields were mutable | Triggers added; result hash covers events |
| 5 | Test guard could be bypassed through period dates or the hypothesis id | Reserved test windows per series; one look per hypothesis; stress runs belong to the same look |
| 6 | Matched baseline not ledgered and not matched | Matched on signals; strategy's own stop/take-profit; summary ledgered |
| 7 | A stop inside the spread filled better than the market | R4.8 entry-bar gap rule |
| 8 | R-multiples scaled by planned risk | R unit defined from realised stop risk (R4.5) |
| 9 | Setup failures were not recorded | Registered as failed experiments |
| 10 | `code_version` ignored untracked files | Fixed |
| 11 | Warm-up bars counted in results | Excluded (R1.1) |
| 12 | Data labels came from the whole series | Taken from the rows actually used |
