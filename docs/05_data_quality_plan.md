# 05 — Data-quality testing plan (XAU/USD)

## Policies
| Situation | Policy |
|---|---|
| **Unfinished candle** | Requests are capped at the last fully closed bar (`end = floor(now / tf) * tf`). Any bar with `close_time > now` is skipped and counted (`unfinished_skipped`). It is never stored. |
| **Missing bars** | Never filled, interpolated or forward-filled in storage. Gaps are reported and classified: *expected* (all missing bars inside a known market closure) or *unexpected*. Any analysis that needs a regular grid must declare its own gap policy in the experiment config. |
| **Invalid candle** (fails an error rule) | Stored with `validation_status = invalid` and issue codes for audit, and excluded from `get_candles` by default. |
| **Unparseable source record** | Not stored as a candle. Kept verbatim in `rejected_records` with the reason. |
| **Same key, identical values** | Counted as a duplicate; no write. |
| **Same key, different values** (source revision) | The stored row is kept unchanged, both versions are written to `candle_conflicts`, and an ERROR is logged. A human decides whether to re-ingest into a new provider/venue namespace. |
| **Synthetic data** | Separate provider and venue, `data_label = synthetic_sample`, CLI banner. Later phases must refuse it for research results. |
| **Timestamps** | Stored as integer ms UTC, open-inclusive and close-exclusive. Naive source times must be declared with a timezone (DST handled by `zoneinfo`). |
| **Trading hours** | `xau_cfd` calendar: weekly closure Fri 17:00 → Sun 18:00 New York; daily break 17:00–18:00 New York, Mon–Thu. Bars inside it are expected closures. |
| **Holidays** | Not modelled yet, so they show as unexpected gaps. That is deliberate: it is safer to review them than to excuse them. |
| **Stale feed** | `STALE_DATA` (last close older than a threshold) and `FLAT_RUN` (≥ 10 bars with o = h = l = c) warnings. |

## Rules and their tests

| Code | Level | Rule | Test |
|---|---|---|---|
| NON_FINITE_PRICE / NON_POSITIVE_PRICE | error | every price is finite and > 0 | `test_invalid_candles_are_flagged` |
| HIGH_BELOW_BODY / LOW_ABOVE_BODY / HIGH_BELOW_LOW | error | low ≤ min(o,c) ≤ max(o,c) ≤ high | same |
| CROSSED_QUOTE | error | bid ≤ ask for each of o/h/l/c where both exist | same, plus `test_bid_ask_equal_is_not_crossed` |
| BASIS_MISMATCH | error | bid-basis primary close equals bid_close | — (constructed by the CSV provider) |
| INVALID_VOLUME / VOLUME_KIND_MISMATCH | error | volume ≥ 0; volume present implies kind ≠ none | `test_invalid_candles_are_flagged` |
| MISALIGNED_OPEN_TIME / BAD_CLOSE_TIME | error | open_time % tf = 0; close = open + tf | same |
| UNFINISHED_CANDLE | error | close_time ≤ evaluation time | `test_unfinished_candle_is_invalid`, `test_unfinished_bars_are_never_stored_…` |
| DUPLICATE_OPEN_TIME | error/warn | unique open_time per series (warn if identical) | `test_duplicate_…` |
| OUT_OF_ORDER | warn | input sorted | `test_out_of_order_is_reported` |
| gaps | warn if unexpected | classified against the session calendar | `test_crypto_gap_…`, `test_fx_weekend_…`, `test_fx_midweek_gap…` |
| BAR_DURING_CLOSURE | warn | no bars inside a calendar closure | `test_bar_inside_fx_closure_is_flagged` |
| gold hours | — | daily break 21:00/22:00 UTC by DST; Sun 18:00 New York reopen; no Friday daily break | `test_xau_*` (3 tests) |
| scope | — | only XAUUSD enabled; out-of-scope or disabled instruments refused | `test_scope_is_xauusd_only`, `test_enabled_instrument_outside_scope…`, `test_disabled_instrument…` |
| PRICE_SPIKE | warn | range ≤ 20× median range (≥ 50 bars) | `test_price_spike_warning` |
| FLAT_RUN / STALE_DATA | warn | see policies | `test_flat_run…`, `test_staleness_threshold` |
| Ingestion: dedupe, conflicts, rejects, resume, abandoned runs, idempotency | — | — | `test_ingest.py` (12 tests) |
| Timezones and DST | — | FX close 21:00/22:00 UTC; MT5-style UTC+2/+3 exports | `test_sessions.py`, `test_csv_naive_times…` |
| End-to-end defects | — | sample file produces exactly the documented issues | `test_synthetic_sample_end_to_end` |
| Live connector (dormant reference) | — | 24 H1 bars, no rejects, idempotent rerun | `test_live_binance.py` (opt-in) |

## Planned additions (Phase 2: gold data pipeline)
- **Broker server-time inference:** locate the weekend gap and daily break in raw server time and solve for the UTC offset
  per DST period. Refuse import if the declared `source_timezone` disagrees.
- **Tick checks:** bid ≤ ask; spread distribution by hour; zero, negative or extreme spreads; out-of-order or duplicate ticks;
  tick → M1 re-aggregation compared against the broker's exported M1 bars (mismatch report).
- **Calendar reconciliation:** observed gaps vs `xau_cfd`; mismatches become broker-hours findings.
- **Cross-broker comparison** (second demo account): mid-price divergence and spread differences.
- **Holiday calendars** for gold (US/UK holidays, early closes).
- **Freshness monitors** for scheduled incremental runs (Phase 6).
