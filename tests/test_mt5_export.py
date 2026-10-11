from __future__ import annotations

import json
import sqlite3

import pytest

from atlas.brokers.server_time import ServerTimeModel
from atlas.config import Instrument
from atlas.ingest import ingest
from atlas.models import PriceBasis, VolumeKind
from atlas.providers.mt5_export import (
    Mt5ExportProvider, Tick, aggregate_bid_bars, parse_bars, parse_ticks, read_text, reconcile_ticks_with_bars,
    spread_profile,
)
from atlas.storage import SCHEMA_VERSION, Store

from .conftest import FixedClock, utc_ms
from .mt5_fixtures import BAR_HEADER, bar_rows, utc_bar_opens, write_export

M1 = 60_000
NYC = ServerTimeModel("ny_close_midnight")
GOLD = Instrument("XAUUSD", "metal_spot", "XAU", "USD", "xau_cfd", 0.01, {"vault_mt5": "XAUUSD"})
WEEK = (utc_ms(2026, 7, 9, 12), utc_ms(2026, 7, 13, 12))  # Thu -> Mon across a summer weekend
CLOCK = FixedClock(utc_ms(2026, 10, 11))


@pytest.fixture
def gold_store():
    s = Store(":memory:")
    s.upsert_instruments([GOLD], 0)
    yield s
    s.close()


def provider(tmp_path, model="ny_close_midnight"):
    return Mt5ExportProvider(name="vault_mt5", venue="vault_markets", directory=tmp_path, server_time_model=model)


def write_week(tmp_path, name="XAUUSD_M1_bars_20260709_20260713.csv", **kw):
    opens = utc_bar_opens(*WEEK, M1)
    return opens, write_export(tmp_path / name, BAR_HEADER, bar_rows(opens, NYC), **kw)


def test_parses_utf16_tab_and_utf8_comma(tmp_path):
    _, f16 = write_week(tmp_path)
    _, f8 = write_week(tmp_path / "u8", encoding="utf-8", delimiter=",")
    b16, r16 = parse_bars(read_text(f16), "a")
    b8, r8 = parse_bars(read_text(f8), "b")
    assert r16 == r8 == [] and len(b16) == len(b8) > 0
    assert [(b.server_open, b.close, b.tick_volume, b.spread_points) for b in b16] == \
           [(b.server_open, b.close, b.tick_volume, b.spread_points) for b in b8]


def test_rejects_non_mt5_files_and_malformed_rows(tmp_path):
    with pytest.raises(ValueError, match="not an MT5 bar export"):
        parse_bars("time,open,high,low,close\n", "x.csv")
    text = "\t".join(BAR_HEADER) + "\n2026.07.09\t12:00:00\t2400.00\t2400.40\t2399.70\t2400.10\t50\t0\t20\n" \
           "2026.07.09\t12:01:00\tN/A\t2400.40\t2399.70\t2400.10\t50\t0\t20\n" \
           "2026.13.09\t12:02:00\t2400.00\t2400.40\t2399.70\t2400.10\t50\t0\t20\n"
    bars, rejects = parse_bars(text, "x.csv")
    assert len(bars) == 1 and len(rejects) == 2 and "x.csv:3" in rejects[0].raw


def test_import_converts_server_time_to_utc_and_is_idempotent(tmp_path, gold_store):
    opens, _ = write_week(tmp_path)
    p = provider(tmp_path)
    result = ingest(gold_store, p, GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert result.status == "succeeded" and result.run["inserted"] == len(opens) and result.run["rejected"] == 0
    stored = gold_store.get_candles("XAUUSD", "vault_mt5", "M1")
    assert [c.open_time for c in stored] == opens  # exact UTC reconstruction
    c = stored[0]
    assert c.volume_kind is VolumeKind.TICK_COUNT and c.price_basis is PriceBasis.BID
    assert c.bid_close == c.close and c.spread_points == 20 and c.ask_close is None  # ask NOT derived from SPREAD
    assert result.report.unexpected_gaps == []  # weekend + daily breaks recognised as closures
    detail = json.loads(gold_store.get_run(result.run_id)["source_detail"])
    assert detail["server_time_status"] == "consistent_unconfirmed"  # no independent evidence configured
    assert len(detail["files"][0]["sha256"]) == 64

    again = ingest(gold_store, provider(tmp_path), GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert (again.run["inserted"], again.run["duplicates"]) == (0, len(opens))


def test_import_refused_on_server_time_mismatch_before_any_write(tmp_path, gold_store):
    write_week(tmp_path)
    result = ingest(gold_store, provider(tmp_path, "fixed:+02:00"), GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert result.status == "failed" and "server-time model rejected" in result.error
    assert gold_store.count("candles") == 0
    detail = json.loads(gold_store.get_run(result.run_id)["source_detail"])
    assert detail["server_time_status"] == "contradicted"
    assert {c["implied_offset_h"] for c in detail["boundary_checks"]} == {3.0}


def test_import_refused_without_a_weekend_to_verify(tmp_path, gold_store):
    opens = utc_bar_opens(utc_ms(2026, 7, 7), utc_ms(2026, 7, 8), M1)
    write_export(tmp_path / "XAUUSD_M1_bars_20260707_20260707.csv", BAR_HEADER, bar_rows(opens, NYC))
    result = ingest(gold_store, provider(tmp_path), GOLD, "M1", utc_ms(2026, 7, 7), utc_ms(2026, 7, 8), clock=CLOCK)
    assert result.status == "failed" and "no week boundary" in result.error
    assert gold_store.count("candles") == 0


def test_overlapping_exports_dedupe_and_conflicts_are_rejected(tmp_path, gold_store):
    opens, _ = write_week(tmp_path)
    rows = bar_rows(opens, NYC)
    rows[100][5] = "9999.99"  # same bar, different close in a second export
    write_export(tmp_path / "XAUUSD_M1_bars_20260709_20260713b.csv", BAR_HEADER, rows)
    result = ingest(gold_store, provider(tmp_path), GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert result.run["inserted"] == len(opens)
    assert result.run["rejected"] == 1
    assert "conflicting duplicate" in gold_store.conn.execute("SELECT reason FROM rejected_records").fetchone()[0]


def test_missing_export_is_a_clear_failure(tmp_path, gold_store):
    result = ingest(gold_store, provider(tmp_path), GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert result.status == "failed" and "no MT5 exports matching" in result.error


def test_v1_database_migrates_to_current_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        CREATE TABLE candles (instrument_id TEXT, provider TEXT, venue TEXT, timeframe TEXT, open_time_utc INTEGER,
            PRIMARY KEY (instrument_id, provider, venue, timeframe, open_time_utc)) WITHOUT ROWID;
        CREATE TABLE ingestion_runs (run_id TEXT PRIMARY KEY);
        INSERT INTO candles VALUES ('XAUUSD', 'p', 'v', 'M1', 0);
    """)
    conn.commit()
    conn.close()
    with Store(path) as s:
        assert s.conn.execute("SELECT value FROM schema_meta").fetchone()[0] == str(SCHEMA_VERSION)
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(candles)")}
        assert "spread_points" in cols
        assert s.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1  # data preserved


# ---- ticks ---------------------------------------------------------------------------------------

TICK_HEADER = "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"


def test_tick_parse_carries_last_known_side_and_rejects_bad_quotes():
    text = TICK_HEADER + (
        "2026.07.08\t15:00:00.100\t2400.10\t\t\t\t2\n"          # bid only: no ask known yet -> rejected
        "2026.07.08\t15:00:00.200\t\t2400.40\t\t\t4\n"          # ask arrives: quote 2400.10/2400.40
        "2026.07.08\t15:00:01.000\t2400.20\t\t\t\t2\n"          # bid moves, ask carried
        "2026.07.08\t15:00:02.000\t2400.50\t2400.30\t\t\t6\n"   # crossed -> rejected
    )
    ticks, rejects = parse_ticks(text, "t.csv", NYC)
    assert [(t.bid, t.ask) for t in ticks] == [(2400.10, 2400.40), (2400.20, 2400.40)]
    assert ticks[0].time_ms == utc_ms(2026, 7, 8, 12) + 200  # server UTC+3 in July
    assert len(rejects) == 2


def test_ticks_rebuild_bars_and_mismatches_are_reported():
    from .conftest import make_candle
    t0 = utc_ms(2026, 7, 8, 12)
    ticks = [Tick(t0 + 1_000, 2400.0, 2400.3), Tick(t0 + 20_000, 2401.0, 2401.3),
             Tick(t0 + 40_000, 2399.5, 2399.8), Tick(t0 + 59_000, 2400.5, 2400.8)]
    assert aggregate_bid_bars(ticks, M1) == {t0: (2400.0, 2401.0, 2399.5, 2400.5)}
    good = make_candle(t0, tf="M1", open=2400.0, high=2401.0, low=2399.5, close=2400.5)
    bad = make_candle(t0, tf="M1", open=2400.0, high=2401.2, low=2399.5, close=2400.5)
    assert reconcile_ticks_with_bars(ticks, [good], tolerance=0.001) == []
    mism = reconcile_ticks_with_bars(ticks, [bad], tolerance=0.001)
    assert [(m.field, m.from_ticks, m.from_bars) for m in mism] == [("high", 2401.0, 2401.2)]


def test_spread_profile_by_utc_hour():
    t0 = utc_ms(2026, 7, 8, 12)
    ticks = [Tick(t0 + i * 1000, 2400.0, 2400.0 + s) for i, s in enumerate([0.2, 0.3, 0.3, 0.9])]
    prof = spread_profile(ticks)
    assert list(prof) == [12]
    assert prof[12]["count"] == 4 and prof[12]["max"] == pytest.approx(0.9) and prof[12]["median"] == pytest.approx(0.3)


def test_server_time_evidence_upgrades_status_to_verified(tmp_path, gold_store):
    write_week(tmp_path)
    p = Mt5ExportProvider(name="vault_mt5", venue="vault_markets", directory=tmp_path,
                          server_time_model="ny_close_midnight",
                          server_time_evidence="support reply 2026-10-13 + server clock screenshot")
    result = ingest(gold_store, p, GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert json.loads(gold_store.get_run(result.run_id)["source_detail"])["server_time_status"] == "verified"


def test_review_finding_3_server_anchored_timeframes_not_accepted(tmp_path, gold_store):
    with pytest.raises(ValueError, match="does not support"):
        ingest(gold_store, provider(tmp_path), GOLD, "D1", WEEK[0], WEEK[1], clock=CLOCK)
    assert {"H4", "D1"}.isdisjoint(provider(tmp_path).timeframes)


def test_review_finding_3_reconcile_refuses_when_nothing_overlaps():
    from .conftest import make_candle
    ticks = [Tick(utc_ms(2026, 7, 8, 12) + 1_000, 2400.0, 2400.3)]
    with pytest.raises(ValueError, match="do not overlap"):
        reconcile_ticks_with_bars(ticks, [make_candle(utc_ms(2026, 7, 9, 12), tf="M1")], tolerance=0.001)
    with pytest.raises(ValueError, match="no bars"):
        reconcile_ticks_with_bars(ticks, [], tolerance=0.001)


def test_review_finding_5_partially_migrated_database_recovers(tmp_path):
    path = tmp_path / "half.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        CREATE TABLE candles (instrument_id TEXT, open_time_utc INTEGER, spread_points INTEGER);
        CREATE TABLE ingestion_runs (run_id TEXT PRIMARY KEY);
    """)  # first ALTER of v2 already applied, version not bumped: the state an interrupted migration leaves
    conn.commit()
    conn.close()
    with Store(path) as s:
        assert s.conn.execute("SELECT value FROM schema_meta").fetchone()[0] == str(SCHEMA_VERSION)
        assert "source_detail" in {r[1] for r in s.conn.execute("PRAGMA table_info(ingestion_runs)")}


def test_review_finding_6_describe_never_loads_or_caches(tmp_path):
    write_week(tmp_path)
    p = provider(tmp_path)
    assert "not loaded" in p.describe("XAUUSD", "M1")["note"]
    assert p._cache == {}


def test_review_finding_7_each_run_records_rejects_and_new_files_are_seen(tmp_path, gold_store):
    opens, _ = write_week(tmp_path)
    rows = bar_rows(opens, NYC)
    rows[5][2] = "N/A"
    write_export(tmp_path / "XAUUSD_M1_bars_20260709_20260713.csv", BAR_HEADER, rows)
    p = provider(tmp_path)
    first = ingest(gold_store, p, GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    second = ingest(gold_store, p, GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)  # same provider instance
    assert first.run["rejected"] == second.run["rejected"] == 1
    write_export(tmp_path / "XAUUSD_M1_bars_20260709_20260713_extra.csv", BAR_HEADER, [rows[5]])
    third = ingest(gold_store, p, GOLD, "M1", WEEK[0], WEEK[1], clock=CLOCK)
    assert third.run["rejected"] == 2  # the new file was picked up despite the cache
    detail = json.loads(gold_store.get_run(third.run_id)["source_detail"])
    assert len(detail["files"]) == 2


def test_review_finding_8_rejected_tick_row_resets_quote_state():
    text = TICK_HEADER + (
        "2026.07.08\t15:00:00.100\t2400.10\t2400.40\t\t\t6\n"   # valid quote
        "2026.07.08\t15:00:00.200\t2400.50\t2400.30\t\t\t6\n"   # crossed -> rejected, state reset
        "2026.07.08\t15:00:00.300\t2400.20\t\t\t\t2\n"          # bid only: ask unknown again -> rejected
        "2026.07.08\tbad-time\t2400.20\t2400.60\t\t\t6\n"       # unparseable -> rejected, state reset
        "2026.07.08\t15:00:00.500\t\t2400.70\t\t\t4\n"          # ask only after reset -> rejected
        "2026.07.08\t15:00:00.600\t2400.30\t2400.65\t\t\t6\n"   # full quote again -> accepted
    )
    ticks, rejects = parse_ticks(text, "t.csv", NYC)
    assert [(t.bid, t.ask) for t in ticks] == [(2400.10, 2400.40), (2400.30, 2400.65)]
    assert len(rejects) == 4
