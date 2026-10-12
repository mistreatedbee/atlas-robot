"""Tick import + spread profile on SYNTHETIC MT5-format files (software validation; not Vault data)."""

from __future__ import annotations

import pytest

from atlas.brokers.server_time import ServerTimeModel
from atlas.config import Instrument
from atlas.events import EconEvent, EventCalendar
from atlas.ingest import ingest
from atlas.providers.mt5_export import Mt5ExportProvider, Tick
from atlas.sessions import XauCfd
from atlas.storage import Store
from atlas.ticks import (
    TickImportError, bar_spread_semantics, import_ticks, load_ticks, spread_profile, suggested_costs,
)

from .conftest import FixedClock, utc_ms
from .mt5_fixtures import BAR_HEADER, bar_rows, server_naive, utc_bar_opens, write_export

NYC = ServerTimeModel("ny_close_midnight")
GOLD = Instrument("XAUUSD", "metal_spot", "XAU", "USD", "xau_cfd", 0.01, {"vault_mt5": "XAUUSD"})
NEWS_AT = utc_ms(2026, 7, 14, 15, 0)


def tick_spread(t_ms: int) -> float:
    if utc_ms(2026, 7, 14, 22, 0) <= t_ms < utc_ms(2026, 7, 14, 22, 15):
        return 0.80  # first 15 min after the daily reopen (18:00 New York = 22:00 UTC in July)
    if abs(t_ms - NEWS_AT) <= 15 * 60_000:
        return 0.60
    return 0.20


def write_ticks(path, start, end, step_ms=5_000):
    cal = XauCfd()
    lines = ["<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"]
    for t in range(start, end, step_ms):
        if not cal.bar_in_session(t, t + 1):
            continue
        s = server_naive(t, NYC)
        bid = 2400.00
        lines.append(f"{s:%Y.%m.%d}\t{s:%H:%M:%S}.000\t{bid:.2f}\t{bid + tick_spread(t):.2f}\t\t\t6")
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_instruments([GOLD], 0)
    # a bar export spanning a weekend verifies the server-time model first
    week = (utc_ms(2026, 7, 9, 12), utc_ms(2026, 7, 13, 12))
    opens = utc_bar_opens(*week, 60_000)
    write_export(tmp_path / "bars" / "XAUUSD_M1_bars_20260709_20260713.csv", BAR_HEADER, bar_rows(opens, NYC))
    p = Mt5ExportProvider(name="vault_mt5", venue="vault_markets", directory=tmp_path / "bars",
                          server_time_model="ny_close_midnight")
    assert ingest(s, p, GOLD, "M1", *week, clock=FixedClock(utc_ms(2026, 10, 1))).status == "succeeded"
    yield s
    s.close()


def imp(store, path, model="ny_close_midnight"):
    return import_ticks(store, path, instrument_id="XAUUSD", provider="vault_mt5", venue="vault_markets",
                        server_time_model=model, now_ms=0)


def test_import_is_idempotent_and_converts_to_utc(store, tmp_path):
    f = write_ticks(tmp_path / "XAUUSD_ticks.csv", utc_ms(2026, 7, 14, 14), utc_ms(2026, 7, 14, 23))
    first = imp(store, f)
    assert first.rows_imported > 5000 and first.rows_rejected == 0 and not first.already_imported
    again = imp(store, f)
    assert again.already_imported and again.rows_imported == 0
    ticks = load_ticks(store, "XAUUSD", "vault_mt5", "vault_markets", utc_ms(2026, 7, 14), utc_ms(2026, 7, 15))
    assert ticks[0].time_ms == utc_ms(2026, 7, 14, 14)  # server UTC+3 converted back to UTC
    assert all(t.time_ms < utc_ms(2026, 7, 14, 21) or t.time_ms >= utc_ms(2026, 7, 14, 22) for t in ticks)


def test_import_refused_without_a_verified_bar_import(tmp_path):
    s = Store(tmp_path / "empty.db")
    f = write_ticks(tmp_path / "x.csv", utc_ms(2026, 7, 14, 14), utc_ms(2026, 7, 14, 15))
    with pytest.raises(TickImportError, match="spans a weekend"):
        imp(s, f)
    s.close()


def test_import_refused_for_a_different_model_than_verified(store, tmp_path):
    f = write_ticks(tmp_path / "x.csv", utc_ms(2026, 7, 14, 14), utc_ms(2026, 7, 14, 15))
    with pytest.raises(TickImportError):
        imp(store, f, model="fixed:+02:00")


def test_spread_profile_and_suggested_costs(store, tmp_path):
    imp(store, write_ticks(tmp_path / "x.csv", utc_ms(2026, 7, 14, 14), utc_ms(2026, 7, 14, 23)))
    ticks = load_ticks(store, "XAUUSD", "vault_mt5", "vault_markets", utc_ms(2026, 7, 14), utc_ms(2026, 7, 15))
    cal = EventCalendar([EconEvent("TEST", "CPI", 1, "x", NEWS_AT, NEWS_AT - 86_400_000, "test", "")])
    prof = spread_profile(ticks, calendar=XauCfd(), reopen_minutes=15, events=cal)
    assert prof["normal"]["p50"] == pytest.approx(0.20) and prof["normal"]["p90"] == pytest.approx(0.20)
    assert prof["reopen_window"]["p90"] == pytest.approx(0.80)
    assert prof["news_window"]["p50"] == pytest.approx(0.60)
    assert prof["normal"]["minutes"] > 400  # time-weighted coverage
    sug = suggested_costs(prof, tick_size=0.01, source="test file")
    assert (sug["base_spread_usd"], sug["reopen_spread_usd"]) == (0.20, 0.80)
    assert "slippage" in sug["note"]


def test_time_weighting_ignores_tick_bursts():
    # 1 minute at 0.20 with ONE quote, then a burst of 100 quotes at 1.00 inside 1 second
    t0 = utc_ms(2026, 7, 14, 14)
    ticks = [Tick(t0, 2400.0, 2400.2)] + [Tick(t0 + 60_000 + k * 10, 2400.0, 2401.0) for k in range(100)]
    ticks.append(Tick(t0 + 61_000, 2400.0, 2400.2))
    prof = spread_profile(ticks, calendar=XauCfd())
    assert prof["normal"]["p50"] == pytest.approx(0.20)  # tick-weighted would say 1.00


def test_bar_spread_column_semantics_detected():
    t0 = utc_ms(2026, 7, 14, 14)
    ticks, bars = [], []
    for m in range(10):
        b = t0 + m * 60_000
        for k, sp in enumerate((0.40, 0.20, 0.30)):
            ticks.append(Tick(b + k * 20_000, 2400.0, round(2400.0 + sp, 2)))
        bars.append({"open_time": b, "close_time": b + 60_000, "spread_points": 20})  # = minimum spread
    res = bar_spread_semantics(bars, ticks, point=0.01)
    assert res["bars_compared"] == 10 and res["best_match"] == "min" and res["mae_points"]["min"] == pytest.approx(0, abs=1e-6)
