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
