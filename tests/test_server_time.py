from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atlas.brokers.server_time import ServerTimeError, ServerTimeModel, infer_offsets, verify_model

from .conftest import utc_ms
from .mt5_fixtures import server_naive, utc_bar_opens

M1 = 60_000
NYC = ServerTimeModel("ny_close_midnight")


def test_ny_close_midnight_is_utc_plus_3_in_us_summer_and_plus_2_in_winter():
    assert NYC.offset_at_utc(utc_ms(2026, 7, 8, 12)) == timedelta(hours=3)
    assert NYC.offset_at_utc(utc_ms(2026, 1, 7, 12)) == timedelta(hours=2)
    # gold's weekly close (Fri 17:00 New York) is exactly server midnight
    assert server_naive(utc_ms(2026, 7, 10, 21), NYC) == datetime(2026, 7, 11, 0, 0)
    assert server_naive(utc_ms(2026, 1, 9, 22), NYC) == datetime(2026, 1, 10, 0, 0)


@pytest.mark.parametrize("spec, utc, server", [
    ("ny_close_midnight", utc_ms(2026, 7, 8, 12), datetime(2026, 7, 8, 15)),
    ("ny_close_midnight", utc_ms(2026, 1, 7, 12), datetime(2026, 1, 7, 14)),
    ("fixed:+02:00", utc_ms(2026, 7, 8, 12), datetime(2026, 7, 8, 14)),
    ("fixed:-05:30", utc_ms(2026, 7, 8, 12), datetime(2026, 7, 8, 6, 30)),
    ("Europe/Athens", utc_ms(2026, 7, 8, 12), datetime(2026, 7, 8, 15)),
])
def test_server_to_utc_roundtrip(spec, utc, server):
    model = ServerTimeModel(spec)
    assert model.to_utc_ms(server) == utc
    assert server_naive(utc, model) == server


def test_unknown_model_rejected():
    with pytest.raises(ServerTimeError):
        ServerTimeModel("GMT+2-ish")


def _opens(model, start, end):
    return [server_naive(t, model) for t in utc_bar_opens(start, end, M1)]


@pytest.mark.parametrize("week, expected_h", [
    ((utc_ms(2026, 7, 9, 12), utc_ms(2026, 7, 13, 12)), 3),   # US summer time
    ((utc_ms(2026, 1, 8, 12), utc_ms(2026, 1, 12, 12)), 2),   # US winter time
])
def test_inference_recovers_offset_from_weekend_close(week, expected_h):
    report = infer_offsets(_opens(NYC, *week), M1)
    assert [o.inferred_offset for o in report.usable] == [timedelta(hours=expected_h)]
    assert verify_model(NYC, report) == []


def test_declared_model_contradicted_by_data_is_reported():
    opens = _opens(NYC, utc_ms(2026, 7, 9, 12), utc_ms(2026, 7, 13, 12))  # broker really is UTC+3 in July
    problems = verify_model(ServerTimeModel("fixed:+02:00"), infer_offsets(opens, M1))
    assert problems and "UTC+03:00" in problems[0] and "UTC+02:00" in problems[0]
    utc_declared = verify_model(ServerTimeModel("fixed:+00:00"), infer_offsets(opens, M1))
    assert utc_declared


def test_eu_dst_gap_weeks_are_caught():
    # Europe/Athens and ny_close_midnight agree most of the year but not between the US (8 Mar) and EU (29 Mar)
    # DST changes in 2026. Data from a ny_close_midnight server in that window contradicts an Athens declaration.
    opens = _opens(NYC, utc_ms(2026, 3, 12, 12), utc_ms(2026, 3, 16, 12))
    assert verify_model(ServerTimeModel("Europe/Athens"), infer_offsets(opens, M1))
    assert verify_model(NYC, infer_offsets(opens, M1)) == []


def test_export_without_weekend_cannot_be_verified():
    opens = _opens(NYC, utc_ms(2026, 7, 7, 0), utc_ms(2026, 7, 8, 0))  # Tue-Wed only
    problems = verify_model(NYC, infer_offsets(opens, M1))
    assert problems and "no usable weekend" in problems[0]


def test_holiday_early_close_is_unusable_not_misread():
    # A broker that closed Thursday (e.g. a holiday) gives a residual far from a whole offset: skipped.
    opens = [datetime(2026, 4, 2, 20, 0) + timedelta(minutes=i) for i in range(30)]
    opens += [datetime(2026, 4, 6, 1, 0) + timedelta(minutes=i) for i in range(30)]
    report = infer_offsets(opens, M1)
    assert report.observations and not report.usable
