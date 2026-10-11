from __future__ import annotations

from collections import Counter

import pytest

from atlas.events import EconEvent, EventCalendar

from .conftest import ROOT, utc_ms

CAL = EventCalendar.load(ROOT / "config/events/us_tier1.csv")
MIN = 60_000


def test_file_loads_with_expected_coverage():
    counts = Counter(e.category for e in CAL.events)
    assert counts == {"CPI": 45, "NFP": 46, "FOMC": 24}
    assert all(e.source.startswith("https://www.") for e in CAL.events)


@pytest.mark.parametrize("event_id, utc", [
    ("CPI-2026-08", utc_ms(2026, 9, 11, 12, 30)),   # 08:30 EDT = 12:30 UTC (verified embargo line)
    ("NFP-2025-09", utc_ms(2025, 11, 20, 13, 30)),  # shutdown-delayed; 08:30 EST = 13:30 UTC (verified)
    ("FOMC-2025-09", utc_ms(2025, 9, 17, 18, 0)),   # 14:00 EDT (verified statement)
    ("CPI-2024-12", utc_ms(2025, 1, 15, 13, 30)),   # winter: 08:30 EST
])
def test_release_times_converted_to_utc_with_dst(event_id, utc):
    (e,) = [x for x in CAL.events if x.event_id == event_id]
    assert e.release_ms == utc


def test_shutdown_gap_is_not_invented():
    ids = {e.event_id for e in CAL.events}
    assert "CPI-2025-10" not in ids and "NFP-2025-10" not in ids


def test_window_lookup():
    cpi = utc_ms(2026, 9, 11, 12, 30)
    e = CAL.window_event(cpi - 10 * MIN, before_ms=15 * MIN, after_ms=15 * MIN, as_of_ms=cpi - 10 * MIN)
    assert e is not None and e.event_id == "CPI-2026-08"
    assert CAL.window_event(cpi - 20 * MIN, before_ms=15 * MIN, after_ms=15 * MIN, as_of_ms=cpi) is None
    assert CAL.window_event(cpi + 14 * MIN, before_ms=15 * MIN, after_ms=15 * MIN, as_of_ms=cpi).event_id == "CPI-2026-08"
    assert CAL.window_event(cpi + 16 * MIN, before_ms=15 * MIN, after_ms=15 * MIN, as_of_ms=cpi) is None


def test_point_in_time_event_not_visible_before_known():
    release = utc_ms(2026, 1, 1, 13, 30)
    cal = EventCalendar([EconEvent("X", "CPI", 1, "r", release, release - 3_600_000, "s", "")])
    assert cal.window_event(release, before_ms=MIN, after_ms=MIN, as_of_ms=release - 2 * 3_600_000) is None
    assert cal.window_event(release, before_ms=MIN, after_ms=MIN, as_of_ms=release - 1_800_000) is not None


def test_invalid_calendars_rejected():
    with pytest.raises(ValueError):
        EventCalendar([EconEvent("X", "CPI", 1, "r", 10, 20, "s", "")])
    with pytest.raises(ValueError):
        EventCalendar([EconEvent("X", "CPI", 1, "r", 10, 0, "s", ""), EconEvent("X", "NFP", 1, "r", 20, 0, "s", "")])
