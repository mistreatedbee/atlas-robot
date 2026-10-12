# Sample dataset — SYNTHETIC, NOT MARKET DATA

`XAUUSD_M5.csv` is **generated** by `scripts/generate_sample_data.py` (seeded random walk, seed `20261011`).
It contains no information about real gold prices, volatility or spreads. Its price level (starting at 2400.00)
and spreads are arbitrary. It exists only so the gold ingestion and validation pipeline can be exercised before the
broker's data is available.

Every candle ingested from it is stored with `provider = sample_synthetic`, `venue = synthetic`,
`data_label = synthetic_sample` and `volume_kind = synthetic`. The CLI prints a warning banner whenever this
data is ingested or displayed.

| Property | Value |
|---|---|
| Window | 2026-08-02 22:00 UTC (Sun 18:00 New York) → 2026-08-28 21:00 UTC (Fri 17:00 New York) |
| Timeframe | M5, bar open time in UTC (ISO-8601 with `Z`) |
| Session model | `xau_cfd`: weekly closure Fri 17:00 → Sun 18:00 New York; daily break 17:00–18:00 New York, Mon–Thu |
| Rows | 5,510 (4 weeks × 115 trading hours × 12 bars − 10 removed bars) |
| Primary prices | Bid OHLC (2 decimals); ask OHLC in `ask_*` (spread US$0.15–0.35; US$0.40–0.90 at 22:00–23:59 UTC) |
| SHA-256 | `cb89d6051493725b0d798d5a02786d5d2d15770d35ba3d57136ba9f43b01a302 (LF line endings on every OS)` |

## Deliberately injected defects

| # | Bar (UTC) | Defect | Expected pipeline behaviour |
|---|---|---|---|
| 1 | 2026-08-12 10:00–10:45 | 10 bars removed | Unexpected gap of 10 bars; nothing filled in |
| 2 | 2026-08-20 14:00 | `close = N/A` | Row rejected, kept verbatim in `rejected_records`; appears as a 1-bar gap |
| 3 | 2026-08-25 09:30 | `high` below open/close and below low | Stored as `invalid`, excluded from research reads; appears as a 1-bar gap |

Expected closures: 19 (3 weekends + 16 daily breaks). All of the above is asserted by
`tests/test_ingest.py::test_synthetic_sample_end_to_end`.
