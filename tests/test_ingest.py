from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.ingest import ingest, validate_range
from atlas.models import DataLabel
from atlas.providers import build_provider

from .conftest import CRYPTO, FX, H1, ROOT, FakeProvider, FixedClock, utc_ms

T0 = utc_ms(2026, 10, 1)
T_END = T0 + 72 * H1  # 3 days, H1
CLOCK = FixedClock(utc_ms(2026, 10, 11))


def test_full_ingestion_stores_every_bar_once(store):
    result = ingest(store, FakeProvider(max_bars=24), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert result.status == "succeeded"
    assert result.run["inserted"] == 72
    assert store.count("candles") == 72
    assert result.report.status.value == "valid"
    assert store.count("quality_reports") == 1


def test_rerun_is_idempotent(store):
    ingest(store, FakeProvider(), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    again = ingest(store, FakeProvider(), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert (again.run["inserted"], again.run["duplicates"]) == (0, 72)
    assert store.count("candles") == 72


def test_failure_mid_run_then_resume_from_checkpoint(store):
    flaky = FakeProvider(max_bars=24, fail_on_call=2)  # first chunk commits, second chunk fails
    failed = ingest(store, flaky, CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert failed.status == "failed" and "simulated provider outage" in failed.error
    assert failed.run["checkpoint_utc"] == T0 + 24 * H1
    assert store.count("candles") == 24

    healthy = FakeProvider(max_bars=24)
    resumed = ingest(store, healthy, CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert resumed.status == "succeeded"
    assert resumed.resumed_from == failed.run_id
    assert healthy.calls[0][0] == T0 + 24 * H1  # did not re-download the committed chunk
    assert resumed.run["inserted"] == 48 and resumed.run["duplicates"] == 0
    assert store.count("candles") == 72
    assert store.get_run(failed.run_id)["status"] == "failed"


def test_crash_leaving_run_marked_running_is_abandoned_and_resumed(store):
    run_id = store.start_run(instrument_id="BTCUSDT", provider="fake", venue="fakevenue", timeframe="H1",
                             source_symbol="BTCUSDT", data_label=DataLabel.LIVE_SOURCE,
                             start_ms=T0, end_ms=T_END, now_ms=T0)
    store.conn.execute("UPDATE ingestion_runs SET checkpoint_utc=? WHERE run_id=?", (T0 + 48 * H1, run_id))
    store.conn.commit()
    provider = FakeProvider()
    result = ingest(store, provider, CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert result.resumed_from == run_id
    assert provider.calls[0][0] == T0 + 48 * H1
    assert store.get_run(run_id)["status"] == "abandoned"


def test_requests_are_capped_at_the_last_closed_bar(store):
    now = T0 + 10 * H1 + H1 // 2  # mid-way through bar 10
    result = ingest(store, FakeProvider(), CRYPTO, "H1", T0, T0 + 24 * H1, clock=FixedClock(now))
    assert result.end_ms == T0 + 10 * H1
    assert store.latest_open_time("BTCUSDT", "fake", "fakevenue", "H1") == T0 + 9 * H1


def test_unfinished_bars_are_never_stored_even_if_clock_moves_back(store):
    # The window is planned at T0+24h, then the clock is corrected backwards (e.g. NTP) to mid bar 10.
    # Bars the provider returns that have not closed by the pipeline's clock must be skipped.
    times = iter([T0 + 24 * H1])
    now = T0 + 10 * H1 + H1 // 2
    clock = lambda: next(times, now)
    result = ingest(store, FakeProvider(max_bars=24), CRYPTO, "H1", T0, T0 + 24 * H1, clock=clock)
    assert result.run["unfinished_skipped"] == 14  # bars 10..23
    assert store.latest_open_time("BTCUSDT", "fake", "fakevenue", "H1") == T0 + 9 * H1
    assert all(c.close_time <= now for c in store.get_candles("BTCUSDT", "fake", "H1", include_invalid=True))


def test_incremental_continues_after_latest_stored(store):
    ingest(store, FakeProvider(), CRYPTO, "H1", T0, T0 + 24 * H1, clock=CLOCK)
    provider = FakeProvider()
    result = ingest(store, provider, CRYPTO, "H1", None, T_END, clock=CLOCK)
    assert provider.calls[0][0] == T0 + 24 * H1
    assert result.run["inserted"] == 48


def test_incremental_without_history_requires_start(store):
    with pytest.raises(ValueError, match="explicit start"):
        ingest(store, FakeProvider(), CRYPTO, "H1", None, T_END, clock=CLOCK)


def test_provider_gaps_are_reported_not_filled(store):
    missing = {T0 + 5 * H1, T0 + 6 * H1}
    result = ingest(store, FakeProvider(missing=missing), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert store.count("candles") == 70
    gaps = result.report.unexpected_gaps
    assert len(gaps) == 1 and gaps[0].missing_bars == 2


def test_rejected_source_rows_are_kept_for_audit(store):
    result = ingest(store, FakeProvider(max_bars=24, rejects_per_call=1), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert result.run["rejected"] == 3
    assert store.count("rejected_records") == 3


def test_source_revision_is_logged_as_conflict(store):
    ingest(store, FakeProvider(), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    result = ingest(store, FakeProvider(price_shift=0.5), CRYPTO, "H1", T0, T_END, clock=CLOCK)
    assert result.run["conflicts"] == 72
    assert store.count("candle_conflicts") == 72


def test_unmapped_instrument_is_refused(store):
    from atlas.config import ConfigError, Instrument
    unmapped = Instrument("ETHUSDT", "crypto_spot", "ETH", "USDT", "crypto_24x7", 0.01, {})
    with pytest.raises(ConfigError):
        ingest(store, FakeProvider(), unmapped, "H1", T0, T_END, clock=CLOCK)


def test_synthetic_sample_end_to_end(store):
    """The committed XAUUSD sample: 5510 rows with three documented defects (data/sample/README.md)."""
    settings = load_settings(ROOT / "atlas.toml")
    provider = build_provider(settings.provider("sample_synthetic"), settings.root)
    gold = settings.instrument("XAUUSD")
    store.upsert_instruments([gold], 0)
    start, end = utc_ms(2026, 8, 2, 22), utc_ms(2026, 8, 28, 21)
    result = ingest(store, provider, gold, "M5", start, end, clock=CLOCK)

    assert result.status == "succeeded"
    assert result.run["rejected"] == 1  # 2026-08-20 14:00 close="N/A"
    assert result.run["invalid"] == 1  # 2026-08-25 09:30 high below body
    assert result.run["inserted"] == 5509
    assert store.data_labels("XAUUSD", "sample_synthetic", "M5") == {"synthetic_sample"}

    summary = result.report.summary()
    unexpected = sorted((g["start"], g["missing_bars"]) for g in summary["gaps"] if not g["expected_closure"])
    assert unexpected == [
        (utc_ms(2026, 8, 12, 10), 10),      # deliberately removed bars
        (utc_ms(2026, 8, 20, 14), 1),       # the rejected malformed row
        (utc_ms(2026, 8, 25, 9, 30), 1),    # the invalid candle, excluded from research reads
    ]
    assert summary["expected_closure_count"] == 3 + 16  # 3 weekends + 4 weeks x Mon-Thu daily breaks
    assert result.report.status.value == "warning"

    report = validate_range(store, gold, "sample_synthetic", "synthetic", "M5", start, end, clock=CLOCK)
    assert report.summary() == summary  # validation is reproducible
