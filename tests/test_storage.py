from __future__ import annotations

import json

from atlas.models import DataLabel, RejectedRecord, ValidationStatus
from atlas.storage import Store

from .conftest import CRYPTO, H1, make_candle, utc_ms

T0 = utc_ms(2026, 10, 7)


def _run(store: Store) -> str:
    return store.start_run(instrument_id="BTCUSDT", provider="fake", venue="fakevenue", timeframe="H1",
                           source_symbol="BTCUSDT", data_label=DataLabel.LIVE_SOURCE,
                           start_ms=T0, end_ms=T0 + 10 * H1, now_ms=T0)


def _commit(store, run_id, candles, status=ValidationStatus.VALID, rejects=()):
    return store.commit_chunk(run_id=run_id, data_label=DataLabel.LIVE_SOURCE,
                              candles=[(c, status, []) for c in candles], rejects=list(rejects),
                              checkpoint_ms=T0 + 10 * H1, now_ms=T0 + 20 * H1, extra_counts={})


def test_roundtrip_preserves_every_field(store):
    run = _run(store)
    c = make_candle(T0, quote_volume=1234.5, trade_count=77, bid_close=101.0, ask_close=101.1)
    _commit(store, run, [c])
    assert store.get_candles("BTCUSDT", "fake", "H1") == [c]


def test_identical_reingest_is_a_duplicate_not_a_second_row(store):
    run = _run(store)
    candles = [make_candle(T0 + i * H1) for i in range(5)]
    assert _commit(store, run, candles).inserted == 5
    counts = _commit(store, _run(store), candles)
    assert (counts.inserted, counts.duplicates, counts.conflicts) == (0, 5, 0)
    assert store.count("candles") == 5


def test_conflicting_values_keep_original_and_record_conflict(store):
    original = make_candle(T0)
    _commit(store, _run(store), [original])
    revised = make_candle(T0, close=100.5)
    counts = _commit(store, _run(store), [revised])
    assert counts.conflicts == 1
    assert store.get_candles("BTCUSDT", "fake", "H1") == [original]
    row = store.conn.execute("SELECT stored_json, incoming_json FROM candle_conflicts").fetchone()
    assert json.loads(row["stored_json"])["close"] == 101.0
    assert json.loads(row["incoming_json"])["close"] == 100.5


def test_invalid_candles_are_stored_but_excluded_by_default(store):
    run = _run(store)
    _commit(store, run, [make_candle(T0)])
    _commit(store, run, [make_candle(T0 + H1, high=50.0)], status=ValidationStatus.INVALID)
    assert len(store.get_candles("BTCUSDT", "fake", "H1")) == 1
    assert len(store.get_candles("BTCUSDT", "fake", "H1", include_invalid=True)) == 2


def test_rejects_and_counters_are_recorded(store):
    run = _run(store)
    _commit(store, run, [make_candle(T0)], rejects=[RejectedRecord("bad", "raw-row")])
    assert store.count("rejected_records") == 1
    r = store.get_run(run)
    assert (r["inserted"], r["rejected"], r["checkpoint_utc"]) == (1, 1, T0 + 10 * H1)


def test_range_queries_are_half_open_and_ordered(store):
    run = _run(store)
    _commit(store, run, [make_candle(T0 + i * H1) for i in (3, 1, 2, 0)])
    got = store.get_candles("BTCUSDT", "fake", "H1", start_ms=T0 + H1, end_ms=T0 + 3 * H1)
    assert [c.open_time for c in got] == [T0 + H1, T0 + 2 * H1]


def test_data_persists_and_is_reproducible_across_reopen(tmp_path):
    path = tmp_path / "atlas.db"
    with Store(path) as s:
        s.upsert_instruments([CRYPTO], 0)
        _commit(s, _run(s), [make_candle(T0 + i * H1, price=100 + i) for i in range(24)])
        first = s.get_candles("BTCUSDT", "fake", "H1")
    with Store(path) as s:
        assert s.get_candles("BTCUSDT", "fake", "H1") == first
        assert s.get_candles("BTCUSDT", "fake", "H1") == first


def test_readable_view_uses_iso_utc(store):
    _commit(store, _run(store), [make_candle(T0)])
    row = store.conn.execute("SELECT open_time, close_time FROM candles_readable").fetchone()
    assert tuple(row) == ("2026-10-07T00:00:00Z", "2026-10-07T01:00:00Z")
