# 01 — Phase 1 technical research report (XAU/USD)

*Updated 2026-10-11 after the scope change to XAU/USD only (doc 00). Statements marked **[verified]** were checked
by a request or test run on this machine that day. Everything else comes from documentation or domain knowledge and must
be re-checked against the chosen broker.*

## 1. Summary
- **The data foundation works and is instrument-agnostic.** Ingestion, validation, storage, crash recovery and
  provenance were proven on real market data (Binance, before the scope change) and on a labelled synthetic XAUUSD
  sample. 102 offline tests pass, plus 1 opt-in live test.
- **Gold-only scope is now enforced in configuration and tests.** XAUUSD is the only registry instrument, there is a gold
  session calendar (daily break plus weekly closure), and loading fails if anything else is enabled.
- **The blocking unknown is the broker.** For a retail XAU/USD CFD, the price feed, spread, contract size, trading hours,
  server time zone and execution rules are all broker-specific. Nothing about costs or tradability can be concluded
  until the broker is chosen and its symbol specification and history are captured (doc 09, D1).
- **There is no "true" gold volume at a retail broker.** Spot gold trades OTC. Broker volume is tick count. The only exchange
  volume nearby is COMEX gold futures (GC), a different contract with licensed data.
- **At R500 the minimum position size is the binding risk constraint.** See section 5.

## 2. What exists (Milestone 1, retained)
| Capability | Where | Gold relevance |
|---|---|---|
| Registry plus scope guard | `config/instruments.toml`, `atlas/config.py` | XAUUSD only; contract spec placeholders |
| Candle model: explicit `volume_kind`, `price_basis`, bid/ask OHLC, `data_label` | `atlas/models.py` | Tick volume is labelled as a proxy, never as traded volume |
| CSV importer with declared source time zone (DST-aware) | `atlas/providers/csv_file.py` | Path for MT5 broker exports (server time often UTC+2/+3) |
| Gold session calendar `xau_cfd` | `atlas/sessions.py` | Daily 17:00–18:00 New York break; Sun 18:00 open |
| Chunked, resumable, idempotent ingestion that never stores unfinished bars | `atlas/ingest.py` | Needed for M1 history |
| Candle and series validation | `atlas/validation.py` | Gaps classified against gold hours |
| SQLite store: runs, checkpoints, conflicts, rejects, quality reports | `atlas/storage.py` | Unchanged |
| Read-only HTTPS client with allowlist and retry/backoff | `atlas/http.py` | Reused by future broker/data APIs |
| Binance connector | `atlas/providers/binance_spot.py` | **Out of scope; dormant reference only** |

### Evidence obtained (2026-10-11)
- Tests: **102 passed, 1 skipped** offline; the live test **passed** with `ATLAS_LIVE_TESTS=1`.
- **Synthetic XAUUSD M5 via CLI:**
  - 5,509 rows received, 1 rejected, 1 invalid, 19 expected closures (3 weekends + 16 daily breaks).
  - 3 unexpected gaps of 10, 1 and 1 bars, exactly the injected defects.
  - Rerun: 0 inserted, 5,509 duplicates.
- **Out-of-scope refusal:** `atlas ingest --provider binance_spot --instrument XAUUSD …` gives
  `error: unknown provider 'binance_spot'`.
- **Before the scope change (real data, Binance):**
  - 720 BTCUSDT H1 bars ingested with 0 issues, and an idempotent rerun.
  - An ETHUSDT M1 run killed with `SIGKILL` at 8,000 rows resumed from its checkpoint and finished at exactly 20,160 unique bars.
  - The in-progress bar was correctly excluded.

  These results prove the pipeline mechanics, which carry over to gold unchanged. That data is archived in
  `data/atlas_pre_gold_scope_crypto_demo.db` and is not used for research.
- **Fixed during the build:**
  - Missing CA bundle on this Mac's Python, fixed with `certifi`; verification is never disabled.
  - Certificate errors were retried pointlessly; they now fail fast.
  - The safety test flagged a docstring; the docstring was reworded and the test was kept strict.

## 3. Gold-specific research findings (to be confirmed with broker data)
- **Market structure:**
  - Spot XAU/USD is OTC. Liquidity concentrates in the London session (including the LBMA price auctions at 10:30 and 15:00 London)
    and the New York session, especially 08:30 New York data releases and the COMEX open.
  - The Asian session is typically thinner.
  - Retail CFD hours usually follow the CME metals schedule: Sun 18:00 → Fri 17:00 New York, with a daily 17:00–18:00 New York
    break. That break falls at 21:00–22:00 UTC in US summer and 22:00–23:00 UTC in winter. *Broker-specific; to verify.*
- **Costs:**
  - Gold spreads are quoted in dollars per ounce and widen sharply after the daily reopen, around US releases and on thin
    holidays.
  - For M1/M5 scalping the spread can be a large share of the typical bar range. Question 2 (doc 00) is therefore the first
    one to answer, using the broker's bid/ask data.
- **Contract conventions vary:**
  - 1.0 lot is commonly 100 oz, so 0.01 lot = 1 oz, but some brokers differ.
  - Digits are 2 or 3.
  - "Pip" for gold is used inconsistently. Atlas will measure everything in **US$ per ounce** and in ticks, never in pips.
- **Macro drivers** (hypotheses, not assumptions):
  - US real yields (10-year TIPS), the dollar index, Fed policy expectations, CPI/PCE inflation, payrolls, central-bank
    gold demand and geopolitical risk.
  - Daily and weekly relationships do not imply a usable M1/M5 edge. That has to be measured.
- **Point-in-time discipline:**
  - US macro series are revised. Backtests must use first-release values (ALFRED vintages) and the official release
    timestamp.
  - News must carry a *first-seen* timestamp.

## 4. Integrity controls (unchanged, apply to gold)
| Risk | Control |
|---|---|
| Unfinished candles | requests capped at the last closed bar, plus the `UNFINISHED_CANDLE` rule |
| Silent revisions | `candle_conflicts`; stored history is never overwritten |
| Fabricated data | gaps reported, never filled; invalid rows kept but excluded |
| Sample mistaken for real | `data_label`, separate provider/venue, CLI banner |
| Broker server-time errors | declared `source_timezone`, DST tests; Phase 2 adds inference from the weekend-gap position |
| Look-ahead / leakage | Phase 3 indicator truncation tests; point-in-time macro data |

## 5. The capital target and position sizing at R500
R500 → R100,000 is 200× in 81 days, or about **6.8% compounded per calendar day**. That is not a defensible objective. Doc 00
makes it a reporting benchmark only.

The minimum position size is a harder, more immediate constraint. It assumes the common convention of 100 oz per lot and
a minimum of 0.01 lot = 1 oz, with US$1 ≈ R16.6 (implied from VALR's BTCZAR / Coinbase BTCUSD prices on 2026-10-11,
approximate):

| Stop distance (1 oz) | Loss at stop | % of R500 |
|---|---|---|
| US$0.30 | ≈ R5 | 1% |
| US$1.00 | ≈ R17 | 3.3% |
| US$3.00 | ≈ R50 | 10% |
| US$5.00 | ≈ R83 | 17% |

A 1%-risk trade would need a stop of about 30 cents, which is roughly the size of the spread itself. Any stop wide
enough to survive normal M1/M5 noise risks several percent or more of the account per trade. The options are:
- a **cent account** or a broker with smaller volume steps (decision D2);
- a larger starting balance; or
- accepting per-trade risk that the risk research is likely to judge unsafe.

Phase 4/5 will quantify this with real spreads and stop distances. It must be settled before demo trading.
