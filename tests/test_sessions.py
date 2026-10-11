from atlas.sessions import AlwaysOpen, FxWeekly, XauCfd, get_calendar

from .conftest import utc_ms

M15 = 900_000
fx = FxWeekly()


def test_fx_closes_friday_21utc_in_us_summer_time():
    # 2026-07-10 is a Friday; New York is on EDT (UTC-4) so 17:00 NY = 21:00 UTC.
    assert fx.bar_in_session(utc_ms(2026, 7, 10, 20, 45), utc_ms(2026, 7, 10, 21))
    assert not fx.bar_in_session(utc_ms(2026, 7, 10, 21), utc_ms(2026, 7, 10, 21, 15))
    assert fx.closure_containing(utc_ms(2026, 7, 11, 12)) == (utc_ms(2026, 7, 10, 21), utc_ms(2026, 7, 12, 21))


def test_fx_closes_friday_22utc_in_us_winter_time():
    # 2026-01-09 is a Friday; New York is on EST (UTC-5) so 17:00 NY = 22:00 UTC.
    assert fx.bar_in_session(utc_ms(2026, 1, 9, 21, 45), utc_ms(2026, 1, 9, 22))
    assert not fx.bar_in_session(utc_ms(2026, 1, 9, 22), utc_ms(2026, 1, 9, 22, 15))
    assert fx.bar_in_session(utc_ms(2026, 1, 11, 22), utc_ms(2026, 1, 11, 22, 15))
    assert not fx.bar_in_session(utc_ms(2026, 1, 11, 21, 45), utc_ms(2026, 1, 11, 22))


def test_fx_weekend_spanning_us_dst_change():
    # US DST ends Sunday 2026-11-01: closure opens Fri 30 Oct 21:00 UTC (EDT), reopens Sun 1 Nov 22:00 UTC (EST).
    assert fx.closure_containing(utc_ms(2026, 10, 31, 12)) == (utc_ms(2026, 10, 30, 21), utc_ms(2026, 11, 1, 22))


def test_bar_straddling_close_counts_as_in_session():
    # H4 bar 20:00-00:00 UTC on a summer Friday is partly open (20:00-21:00).
    assert fx.bar_in_session(utc_ms(2026, 7, 10, 20), utc_ms(2026, 7, 11, 0))


def test_midweek_is_open():
    assert fx.closure_containing(utc_ms(2026, 10, 7, 3)) is None


def test_crypto_always_open():
    assert AlwaysOpen().bar_in_session(utc_ms(2026, 10, 10, 12), utc_ms(2026, 10, 10, 12, 15))
    assert get_calendar("crypto_24x7").name == "crypto_24x7"


xau = XauCfd()


def test_xau_daily_break_summer_and_winter():
    # Mon-Thu 17:00-18:00 New York: 21:00-22:00 UTC under EDT, 22:00-23:00 UTC under EST.
    assert xau.closure_containing(utc_ms(2026, 7, 14, 21, 30)) == (utc_ms(2026, 7, 14, 21), utc_ms(2026, 7, 14, 22))
    assert xau.closure_containing(utc_ms(2026, 1, 13, 22, 30)) == (utc_ms(2026, 1, 13, 22), utc_ms(2026, 1, 13, 23))
    assert xau.bar_in_session(utc_ms(2026, 7, 14, 20, 55), utc_ms(2026, 7, 14, 21))
    assert not xau.bar_in_session(utc_ms(2026, 7, 14, 21, 55), utc_ms(2026, 7, 14, 22))
    assert xau.bar_in_session(utc_ms(2026, 7, 14, 22), utc_ms(2026, 7, 14, 22, 5))


def test_xau_weekend_reopens_sunday_18_new_york():
    # Fri 2026-07-10 21:00 UTC -> Sun 2026-07-12 22:00 UTC (18:00 EDT), one hour later than the FX convention.
    assert xau.closure_containing(utc_ms(2026, 7, 11, 12)) == (utc_ms(2026, 7, 10, 21), utc_ms(2026, 7, 12, 22))
    assert not xau.bar_in_session(utc_ms(2026, 7, 12, 21, 30), utc_ms(2026, 7, 12, 21, 35))
    assert get_calendar("xau_cfd") is not None


def test_xau_no_daily_break_on_friday():
    # Friday 17:00 NY is the weekly close, so the window found is the weekly one, not a 1-hour break.
    start, end = xau.closure_containing(utc_ms(2026, 7, 10, 21, 30))
    assert end - start == 49 * 3_600_000
