# 00 — Project scope: XAU/USD only (authoritative)

**Scope change recorded 2026-10-11.** Atlas researches, and may eventually trade, exactly one instrument:
**spot gold against the US dollar (XAU/USD)**, as offered by the chosen broker. Crypto, other FX pairs, indices,
equities and all other instruments are out of scope. Where any other document conflicts with this one, this one wins.

## How the scope is enforced in code
- `atlas.toml` → `[scope] authorised_instruments = ["XAUUSD"]`. Loading fails if any *enabled* registry instrument
  is outside this list (`check_scope`, tested).
- `config/instruments.toml` contains only XAUUSD.
- `Settings.instrument()` refuses disabled instruments.
- The Binance connector built in Milestone 1 is kept as a dormant, tested reference implementation of the provider
  interface. It is not configured, so the CLI cannot use it (`unknown provider 'binance_spot'`). Delete it if you prefer.
- The machinery stays generic (registry, calendars, CSV importer). That is cheap, and it keeps gold-specific facts in configuration
  where they can be verified per broker, rather than hard-coded.

## Research questions (hypotheses to test, not assumptions)
1. Which XAU/USD conditions are associated with the most reliable short-term opportunities?
2. How do spread and volatility affect the feasibility of scalping gold at the chosen broker?
3. Do particular sessions or overlaps provide a measurable advantage **after costs**?
4. How does gold behave around scheduled US releases (CPI, NFP, FOMC, PCE), and when is execution risk too high?
5. Can volatility/regime classification filter out low-quality trades out-of-sample?
6. Which indicators add information beyond raw price action (measured by incremental out-of-sample value)?
7. Does multi-timeframe confirmation improve out-of-sample results?
8. How robust are strategies to a different broker's feed, wider spreads and execution delay?
9. Does combining the specialist agents beat a simple, well-defined baseline strategy?
10. When is the statistically justified decision **not to trade**?

## Data requirements to establish before downloading (Phase 2 checklist)
| Item | Source of truth | Status |
|---|---|---|
| Intended broker and account type (standard / raw / cent) | Your decision (doc 09, D1) | **open** |
| Exact symbol name (e.g. `XAUUSD`, `XAUUSD.m`, `GOLD`) | Broker symbol list | open |
| Contract size (oz per lot), tick size, digits | MT5 symbol specification | placeholders in registry, **unconfirmed** |
| Minimum volume and volume step | MT5 specification | unconfirmed |
| Minimum stop distance (stops level), freeze level | MT5 specification | open |
| Margin requirement / leverage for gold | Broker and account | open |
| Trading hours, daily break, holiday schedule | Broker's published hours, checked against data gaps | modelled as CME-style `xau_cfd`, **to verify** |
| Server time zone and DST rule | Broker; inferred from weekend-gap position | open |
| M1/M5 history depth; higher timeframes | MT5 history download | open |
| Tick and bid/ask history | MT5 ticks or broker API | open |
| Spread (typical, by hour, around news), commission, swap | Broker plus recorded ticks | open |
| Execution rules (market/instant execution, requotes, hedging allowed, FIFO) | Broker | open |

## Operating principles (binding on all later phases)
- Continuous operation does not mean continuous trading. Staying flat is a valid, often correct, outcome.
- No martingale, no unrestricted grid averaging, no loss-recovery sizing, no uncontrolled leverage.
- All open XAU/USD positions are **one combined exposure**. Correlated or duplicate entries are never treated as
  independent.
- Deterministic risk controls that no AI agent can override.
- Trading stops if prices are stale, data is unreliable or account state cannot be verified.
- The R500 → R100,000 aspiration is reported against honestly. It is **never** an input to position sizing or risk limits.
- No phase is skipped. Live execution requires passing readiness checks **and** your explicit authorisation.
