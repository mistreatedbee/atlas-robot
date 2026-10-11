from __future__ import annotations

import math
from dataclasses import replace

import pytest

from atlas.models import ValidationStatus, VolumeKind
from atlas.sessions import AlwaysOpen, FxWeekly
from atlas.validation import Severity, status_for, validate_candle, validate_series

from .conftest import H1, make_candle, utc_ms

T0 = utc_ms(2026, 10, 7, 0)  # a Wednesday
NOW = utc_ms(2026, 10, 11)


def codes(issues):
    return {i.code for i in issues}


def test_well_formed_candle_is_valid():
    issues = validate_candle(make_candle(T0), NOW)
    assert issues == []
    assert status_for(issues) is ValidationStatus.VALID


@pytest.mark.parametrize("overrides, code", [
    ({"high": 99.0}, "HIGH_BELOW_BODY"),          # high below open/close
    ({"low": 102.0}, "LOW_ABOVE_BODY"),           # low above open/close
    ({"high": 97.0, "low": 98.0}, "HIGH_BELOW_LOW"),
    ({"close": -1.0}, "NON_POSITIVE_PRICE"),
    ({"open": 0.0}, "NON_POSITIVE_PRICE"),
    ({"close": math.nan}, "NON_FINITE_PRICE"),
    ({"high": math.inf}, "NON_FINITE_PRICE"),
    ({"volume": -5.0}, "INVALID_VOLUME"),
    ({"volume_kind": VolumeKind.NONE}, "VOLUME_KIND_MISMATCH"),
    ({"open_time": T0 + 60_000, "close_time": T0 + 60_000 + H1}, "MISALIGNED_OPEN_TIME"),
    ({"close_time": T0 + H1 - 1}, "BAD_CLOSE_TIME"),
    ({"bid_close": 101.0, "ask_close": 100.9}, "CROSSED_QUOTE"),
])
def test_invalid_candles_are_flagged(overrides, code):
    overrides = dict(overrides)
    issues = validate_candle(make_candle(overrides.pop("open_time", T0), **overrides), NOW)
    assert code in codes(issues)
    assert status_for(issues) is ValidationStatus.INVALID


def test_unfinished_candle_is_invalid():
    now = T0 + H1 // 2  # half-way through the bar
    issues = validate_candle(make_candle(T0), now)
    assert "UNFINISHED_CANDLE" in codes(issues)


def test_candle_closing_exactly_now_is_complete():
    assert validate_candle(make_candle(T0), T0 + H1) == []


def test_bid_ask_equal_is_not_crossed():
    assert validate_candle(make_candle(T0, bid_close=101.0, ask_close=101.0), NOW) == []


def series(times, **kw):
    return [make_candle(t, price=100 + i % 5, **kw) for i, t in enumerate(times)]


def test_contiguous_series_is_valid():
    report = validate_series(series(range(T0, T0 + 10 * H1, H1)), AlwaysOpen(), NOW)
    assert report.status is ValidationStatus.VALID
    assert report.gaps == []


def test_crypto_gap_is_unexpected_and_never_filled():
    times = [T0, T0 + H1, T0 + 5 * H1]
    candles = series(times)
    report = validate_series(candles, AlwaysOpen(), NOW)
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert (gap.start, gap.end, gap.missing_bars, gap.expected_closure) == (T0 + 2 * H1, T0 + 5 * H1, 3, False)
    assert report.status is ValidationStatus.WARNING
    assert len(candles) == 3  # nothing was synthesised


def test_duplicate_with_different_values_is_an_error():
    a = make_candle(T0)
    b = make_candle(T0, close=100.5)
    report = validate_series([a, b], AlwaysOpen(), NOW)
    dup = [i for i in report.issues if i.code == "DUPLICATE_OPEN_TIME"]
    assert dup and dup[0].severity is Severity.ERROR
    assert report.status is ValidationStatus.INVALID


def test_identical_duplicate_is_a_warning():
    a = make_candle(T0)
    report = validate_series([a, a], AlwaysOpen(), NOW)
    assert [i.severity for i in report.issues if i.code == "DUPLICATE_OPEN_TIME"] == [Severity.WARNING]


def test_out_of_order_is_reported():
    report = validate_series(series([T0 + H1, T0]), AlwaysOpen(), NOW)
    assert "OUT_OF_ORDER" in codes(report.issues)


def test_mixed_series_rejected():
    with pytest.raises(ValueError):
        validate_series([make_candle(T0), make_candle(T0 + H1, instrument_id="ETHUSDT")], AlwaysOpen(), NOW)


def test_fx_weekend_is_expected_closure_not_a_gap_error():
    # Friday 2026-10-09 20:00 UTC (16:00 EDT) is the last H1 bar; Sunday 21:00 UTC (17:00 EDT) reopens.
    fri_last = utc_ms(2026, 10, 9, 20)
    sun_open = utc_ms(2026, 10, 11, 21)
    report = validate_series(series([fri_last - H1, fri_last, sun_open]), FxWeekly(), utc_ms(2026, 10, 12))
    assert len(report.gaps) == 1 and report.gaps[0].expected_closure
    assert report.status is ValidationStatus.VALID


def test_fx_weekend_gap_that_starts_early_is_unexpected():
    # Missing the Friday 20:00 UTC bar (still in session) makes the weekend gap partly unexpected.
    fri = utc_ms(2026, 10, 9, 19)
    sun_open = utc_ms(2026, 10, 11, 21)
    report = validate_series(series([fri, sun_open]), FxWeekly(), utc_ms(2026, 10, 12))
    assert report.unexpected_gaps


def test_fx_midweek_gap_is_unexpected():
    report = validate_series(series([T0, T0 + 3 * H1]), FxWeekly(), NOW)
    assert report.unexpected_gaps and report.unexpected_gaps[0].missing_bars == 2


def test_bar_inside_fx_closure_is_flagged():
    sat = utc_ms(2026, 10, 10, 12)
    report = validate_series(series([sat]), FxWeekly(), NOW)
    assert "BAR_DURING_CLOSURE" in codes(report.issues)


def test_price_spike_warning():
    candles = series(range(T0, T0 + 60 * H1, H1))
    candles[30] = replace(candles[30], high=candles[30].high + 500)
    report = validate_series(candles, AlwaysOpen(), NOW)
    spikes = [i for i in report.issues if i.code == "PRICE_SPIKE"]
    assert [i.open_time for i in spikes] == [candles[30].open_time]


def test_flat_run_suggests_stale_feed():
    flat = [make_candle(t, open=100.0, high=100.0, low=100.0, close=100.0) for t in range(T0, T0 + 12 * H1, H1)]
    report = validate_series(flat, AlwaysOpen(), NOW)
    assert "FLAT_RUN" in codes(report.issues)


def test_staleness_threshold():
    candles = series([T0])
    assert "STALE_DATA" in codes(validate_series(candles, AlwaysOpen(), NOW, max_staleness_ms=H1).issues)
    assert "STALE_DATA" not in codes(validate_series(candles, AlwaysOpen(), T0 + H1, max_staleness_ms=H1).issues)


def test_empty_series():
    report = validate_series([], AlwaysOpen(), NOW)
    assert "EMPTY_SERIES" in codes(report.issues)
