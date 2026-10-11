# 02 — Candidate data sources for XAU/USD research

**Key:**
- **V** = request succeeded from this machine on 2026-10-11.
- **D** = provider documentation as I know it; re-check before use.
- **?** = uncertain.

Licensing is not legal advice: read each provider's terms before storing data long-term.

## A. Gold prices (execution-relevant)

| Source | Purpose | Access | Coverage | Limitations | Licensing | Credentials | Status |
|---|---|---|---|---|---|---|---|
| **Chosen broker: MT5 history / ticks** | Ground truth for the venue Atlas would trade: bid/ask, spread, hours, contract spec | MT5 terminal export (bars, ticks) or broker API; imported via `user_csv` | M1 usually available for years; tick depth varies by broker | Tick volume only; server time zone (often UTC+2/+3); symbol suffixes; history limited by server | Account agreement, personal use | Broker account (demo is enough) | **Primary**, broker not chosen; importer ready |
| OANDA v20 (`XAU_USD`) | Bid/ask/mid candles by API | REST with token | Long history, S5 upwards, ≤ 5,000 per request (D) | Availability to SA residents **?**; OANDA's own feed | Account terms | Demo token | D, ? |
| Dukascopy (`XAU/USD`) | Long tick bid/ask history for broker-independence tests | JForex / website; unofficial bulk endpoints | Ticks for many years (D) | Different venue from your broker; bulk-download terms **?** | Restrictive (?) | None for website | D, ? |
| HistData.com (`XAUUSD`) | Free M1 bars for a quick start | Manual zips | M1 since ~2009 (D) | **Bid only**, fixed EST without DST (D) | Personal use | None | D |
| Second broker's MT5 demo | Cross-broker robustness (question 8) | MT5 export | As above | Different spreads and hours | Account agreement | Demo account | Recommended later |

## B. Gold reference and context data (not execution prices)

| Source | Purpose | Access | Limitations | Credentials | Status |
|---|---|---|---|---|---|
| COMEX gold futures (GC), e.g. via Databento or the CME DataMine | Real exchange volume and open interest; futures-spot basis | Paid API / files | Different contract (roll, basis); licensed | Paid key | D |
| LBMA Gold Price (AM/PM auctions) | Benchmark fixes, session anchors | ICE/LBMA licensed | **Licensed**: redistribution restricted; removed from FRED for that reason (D) | Licence | D |
| CFTC Commitments of Traders (gold) | Positioning context (weekly) | Free CSV | Weekly; Tuesday data released Friday, so it must be aligned to the release time | None | D |
| World Gold Council: Gold Demand Trends / central-bank purchases | Structural demand context | Reports / downloads | Quarterly/monthly, revised; slow-moving | Registration for some data | D |
| SPDR Gold Trust (GLD) holdings | ETF flow context | Daily file on the issuer's site | Daily; publication time must be recorded | None | D |

## C. US macro and events

| Source | Purpose | Access | Notes | Credentials | Status |
|---|---|---|---|---|---|
| **FRED / ALFRED** | DGS10 (10-year nominal), DFII10 (10-year TIPS real yield), T10YIE (breakeven), DTWEXBGS (broad dollar), FEDFUNDS; vintages for point-in-time values | REST | Daily series are not intraday; ALFRED vintages are needed for revised data (CPI, NFP, PCE) | Free API key | **V** (key required) |
| BLS release schedule (CPI, Employment Situation) | Exact 08:30 New York release times | Published calendars | Primary source of timestamps | None | D |
| BEA schedule (PCE, GDP) | Release times | Published calendars | — | None | D |
| Federal Reserve: FOMC calendar, statements, minutes, SEP | Decision times (14:00 New York statement, press conference) | Official website/RSS | Unscheduled actions must be captured too | None | D |
| Economic calendar with consensus (Trading Economics, Finnhub, other paid feeds) | "Surprise" = actual − consensus | Paid API | Free calendar sites forbid scraping; consensus needs a licence | Paid | Decision D4 |
| ECB data portal | EUR context (dollar-index driver) | SDMX REST | — | None | **V** |
| News (GDELT, official wires, Reuters/Bloomberg if licensed) | Geopolitical risk events | API / RSS | First-seen timestamp mandatory; separate fact from interpretation | Varies | D |

## D. Out of scope (kept only as verified evidence of the provider framework)
Binance Spot REST (`data-api.binance.vision`) **V**, plus Coinbase, Kraken, Bybit and VALR **V**. Crypto is not
researched or traded. Binance's tokenised-gold pairs (e.g. PAXG) are **not** XAU/USD and must not be used as a proxy.

## Recommendation for Phase 2
1. Choose the broker (D1) and open a **demo** account. No live credentials are needed.
2. Capture the XAUUSD symbol specification (contract, digits, volume min/step, stops level, margin, swap, hours) as a
   versioned file in the repo.
3. Export M1 bars **and** ticks with bid/ask for as long a history as the server provides. Import them via `user_csv` with
   the verified server time zone.
4. Add FRED/ALFRED (free key) for DFII10, DGS10 and DTWEXBGS, and official release calendars, in Phase 3.
