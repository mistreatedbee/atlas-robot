# 06 — XAU/USD indicator and feature catalogue (Phase 3 implementation)

All distances are in US$ per ounce. Features are computed on broker **bid** (or mid, if declared) M1/M5 bars, with higher-timeframe
context taken only from the last **closed** M15/H1/H4 bar.

**Rules for every indicator:**
- **Causality:** the value at bar *t* uses only bars ≤ *t*, and only *completed* bars. A signal computed on bar *t*'s close can be
  acted on at bar *t+1*'s open at the earliest.
- **Warm-up:** values are `NaN` until warm-up is complete. Recursive indicators (EMA, RSI-Wilder, ATR, ADX) also
  depend on the seed, so they need ≥ 3–5× the period of history before values are treated as stable.
- **Reference tests:** verify against an established implementation (TA-Lib, or pandas-ta for cross-checks) on a fixed
  fixture, plus hand-computed small examples.
- **Look-ahead test:** compute on the full series and on the series truncated at *t*. Values at *t* must be identical.
- **Gaps:** indicators are computed per contiguous segment, or the experiment must declare how gaps are handled. Bars are never
  invented.
- **No indicator is a signal by itself.** Combinations need a pre-registered hypothesis (doc 07).

| Indicator | Definition (period *n*) | Inputs | Warm-up | Notes and limitations |
|---|---|---|---|---|
| SMA | mean of last *n* closes | close | n−1 bars | Lags by about n/2 bars |
| EMA | αₜ·x + (1−α)·EMAₜ₋₁, α = 2/(n+1), seeded with SMA(n) | close | n−1 (+ stabilisation) | Seed convention differs between libraries, so the test must pin it |
| RSI (Wilder) | 100 − 100/(1+RS), RS = Wilder-smoothed gain / loss | close | n bars (+ stabilisation) | Bounded; "overbought" is not a sell signal in trends |
| MACD | EMA(12) − EMA(26); signal = EMA(9) of MACD; histogram = difference | close | ~33 bars (+ stabilisation) | Derived from EMAs, so it lags and is redundant with them |
| ATR (Wilder) | Wilder average of TR = max(h−l, \|h−c₋₁\|, \|l−c₋₁\|) | h, l, c | n bars | The Sunday reopen and the daily-break reopen inflate TR; the handling must be declared |
| Bollinger Bands | SMA(n) ± k·σ(n), using population σ (pin ddof) | close | n−1 | ddof convention differs between libraries |
| Stochastic %K/%D | %K = 100·(c − LLₙ)/(HHₙ − LLₙ); %D = SMA₃(%K) | h, l, c | n−1 (+2) | Division by zero on flat ranges must yield NaN |
| ADX / +DI / −DI | Wilder-smoothed directional movement / ATR; ADX = Wilder avg of DX | h, l, c | ~2n bars (+ stabilisation) | Slow; highly seed-dependent |
| VWAP (session) | Σ(typical price·volume) / Σ volume since session start | h, l, c, volume | from the session anchor | **Gold CFD feeds have only broker tick volume**, so a broker "VWAP" is a tick-weighted proxy and must be labelled as one. Real volume exists only for COMEX futures. The anchor (UTC day or session) must be explicit. |
| Pivot levels (classic) | P = (H+L+C)/3 of the *previous completed* period; R1 = 2P−L, S1 = 2P−H … | prior period h, l, c | 1 full period | the gold "day" should run from 18:00 to 17:00 New York (the broker trading day), not 00:00 UTC; this must be declared |
| Tick-activity features | tick-count z-score, relative to the same time of day | tick volume | n bars | Broker-specific proxy for activity, **not** traded volume |
| Candle / price-action features | body/range ratio, upper/lower wick ratios, inside/outside bar, engulfing, gap from the previous close | o, h, l, c | 1–2 bars | Pattern definitions must be exact (tolerances in ticks) |
| Volatility / regime | realised vol (std of log returns), ATR/price, Parkinson / Garman–Klass estimators, vol percentile, trend vs range (ADX threshold, efficiency ratio) | o, h, l, c | n bars (percentiles: lookback window) | Percentiles must use **trailing** windows only |
| Session features | session ID (Asia/London/NY), overlap flag, minutes since the daily reopen, LBMA auction windows, minutes to/from the next tier-1 US release | timestamps, calendars | none | Depends on correct US/UK DST and holiday calendars |
| Market structure | swing highs/lows (fractal with *k* bars **confirmed** only after *k* later bars), HH/HL/LH/LL state, break of structure | h, l | k bars confirmation lag | Swings are only known after confirmation; using them earlier is look-ahead |
| Support/resistance zones | clustered prior swing levels, prior-day high/low, session high/low, round numbers (US$5/10) | swings, sessions | lookback | Zone width in US$ or ATR; levels must exist before the bar that tests them |
| Breakout / failed breakout | close beyond a zone by ≥ x·ATR; failure = re-entry within N bars | zones, ATR | as inputs | Definition fixed before testing |
| Spread features | current spread, spread percentile by hour | bid/ask | lookback | Doubles as a no-trade filter |
| Macro context (daily) | change in DFII10, DGS10, DTWEXBGS over prior days (point-in-time) | FRED/ALFRED | days | Daily data cannot time M1 entries; context only |

**Reference-implementation decision (doc 09):** TA-Lib is the industry reference but needs a C library. A pure-Python
NumPy implementation with tests against stored TA-Lib output avoids the runtime dependency.
