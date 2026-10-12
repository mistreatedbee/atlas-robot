# 12 — Decision log

Engineering decisions made under the CTO directive. Each entry records why the decision was made and how to reverse it.

| # | Date | Decision | Why | Reversal |
|---|---|---|---|---|
| 1 | 2026-10-11 | Local git repository, no remote | Reproducibility needs commit hashes; a remote is the owner's choice (data/privacy) | Add a private remote on request |
| 2 | 2026-10-11 | `.gitignore` excludes all `data/*.db`, `data/imports/*`, logs, `.env`, keys, kill-switch file | Keep broker data, account artefacts and secrets out of history | — |
| 3 | 2026-10-11 | Broker spec has explicit `status`; Vault spec stays **unverified** with no numbers; generic values live in a separate PROVISIONAL file | Lets development continue without a provisional value ever being mistaken for a Vault fact | Replace the Vault file with verified values; no code change |
| 4 | 2026-10-11 | Server time must be checked against **both** weekend boundaries; a wrong model is refused; "verified" requires independent evidence | The independent review showed that a one-sided check could approve a wrong offset | — |
| 5 | 2026-10-11 | MT5 importer accepts M1–H1 only | MT5 builds H4/D1 on server-time boundaries, which don't align with UTC bars | Add server-anchored bar support if ever needed (higher timeframes can be resampled from M1 instead) |
| 6 | 2026-10-11 | MT5 `<SPREAD>` column stored but **not** used as a cost | Its meaning (bar minimum vs other) is unverified; using it could understate costs | Use it after verifying against tick exports |
| 7 | 2026-10-11 | Risk gate: hard ceilings in code (2% per trade, 5% daily, 25% drawdown, 50% margin); defaults 1% / 3% / 10% in `config/risk.toml` | No agent or config edit can raise risk past a reviewed bound | Ceilings change only by a reviewed code change |
| 8 | 2026-10-11 | Only BACKTEST and PAPER modes exist | Live execution requires the owner's explicit authorisation and a separate, reviewed activation path | Built in the live-readiness phase, after authorisation |
| 9 | 2026-10-11 | Sizing takes no loss history | Makes martingale / loss-recovery sizing structurally impossible | — |
| 10 | 2026-10-11 | NumPy added as a dependency | Feature and backtest computation over millions of M1 bars | Pure-Python fallback is possible but slow |
| 11 | 2026-10-11 | Indicators: SMA-seeded EMA, Wilder smoothing, population σ; NaN restarts warm-up; expected closures don't break segments by default | Pins the conventions that differ between libraries; never bridges missing data | The gap policy is per-experiment configuration |
| 12 | 2026-10-11 | Independent review by a separate agent before a milestone is considered complete | It caught 3 high-severity issues in D1 that the author's tests missed | — |
| 13 | 2026-10-11 | Simulation rules written and independently challenged BEFORE implementation (docs/13 v1 → v2) | 15 review findings, including look-ahead in the draft's own gap-exit rule | Rules change only through a new reviewed version |
| 14 | 2026-10-11 | Exits before closures come from the calendar only; unexpected gaps are held and flagged | Exiting before an unknowable gap is look-ahead | — |
| 15 | 2026-10-11 | Market orders fill at the next existing bar within 5 min, never across a scheduled closure | MT5 omits quiet minutes; never filling across a closure avoids fictitious gap fills | `max_fill_wait_minutes` per experiment |
| 16 | 2026-10-11 | Constant USD→ZAR rate in simulations, labelled; USD P&L reported alongside | No daily FX series yet | Daily FX series (backlog) |
| 17 | 2026-10-11 | Ledger is append-only through SQLite triggers; test-set access limited per **hypothesis**, not per parameter set | Prevents silent editing and repeated test-set peeking | Override exists but is recorded |
| 18 | 2026-10-11 | Safety scan matches HTTP verbs as quoted literals and adds urllib implicit-POST patterns | `\bDELETE\b` falsely matched SQL triggers; the new patterns also catch `Request(data=...)`, which the old ones missed | — |
| 19 | 2026-10-11 | The leakage harness compares the strategy's raw decisions at the cut bar | Caught a blind spot (one-bar peek undetected) and avoided a false positive for positions open at the cut | — |
| 20 | 2026-10-11 | Repository published to a public GitHub repo, by the owner's decision | Owner chose "push to the public repo as-is" after being told about the visibility | Make the repo private in GitHub settings |
| 21 | 2026-10-11 | Test windows reserved per **instrument**, atomically with registration; a look includes the code version; only ×1/×1.5/×2 stress runs on the test set | The re-check showed the look rule could be dodged through other feeds, timeframes or multipliers | — |
| 22 | 2026-10-11 | Event dates transcribed from official BLS/Fed pages; `known_since` = release − 1 day | Primary sources; deliberately late knowledge time so nothing is known earlier than it plausibly was | Replace with recorded publication dates if obtained |
| 23 | 2026-10-11 | USD/ZAR from ECB reference rates, usable from 17:00 Berlin on the reference day | Free, no credentials, verified live; a late availability time avoids look-ahead. These are not the broker's conversion rates (limitation) | Use Vault's conversion rates when known |
| 24 | 2026-10-12 | News: no entries ±15 min around known tier-1 releases, flat before news, slippage ×3 (provisional) | Scalping research must not assume normal costs through CPI/NFP/FOMC | Window lengths and multiplier from Vault tick data |
| 25 | 2026-10-12 | Test-set "look" identified by a content hash of the `atlas/` code | Stable across docs commits and untracked notes; changes with any code edit, committed or not | — |
| 26 | 2026-10-12 | Safety scan for request bodies uses the syntax tree, not regexes | No false positives on our own code; catches positional, keyword and unpacked bodies, plus network imports | — |
| 27 | 2026-10-12 | Backtester milestone marked complete after 3 independent review rounds (29 findings fixed); the last round's fixes are covered by regression tests but were not re-reviewed | Diminishing returns; the residual risks are listed in docs/13 | Re-review on request |
| 28 | 2026-10-12 | Broker access only through the local MT5 terminal via the official MetaTrader5 package (Windows); the read-only stage requires an investor-password login on real accounts | Official, documented interface; credentials stay in the terminal; the broker enforces read-only access | — |
| 29 | 2026-10-12 | The MT5 package may be imported in exactly one file, guarded by an allowlist proxy plus AST tests | Narrow, verifiable exception instead of a blanket ban that would block the integration | — |
| 30 | 2026-10-12 | Cross-platform: tzdata, explicit UTF-8, LF line endings, CI on Windows/macOS/Linux | The owner asked for every device; CI proves it on each push | — |
