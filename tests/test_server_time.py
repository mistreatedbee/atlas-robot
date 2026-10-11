from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from atlas.brokers.server_time import (
    ServerTimeError, ServerTimeModel, SessionAnchors, Verdict, verify_server_time,
)

from .conftest import utc_ms
from .mt5_fixtures import server_naive, utc_bar_opens

M1 = 60_000
NYC = ServerTimeModel("ny_close_midnight")


def test_ny_close_midnight_is_utc_plus_3_in_us_summer_and_plus_2_in_winter():
    assert NYC.offset_at_utc(utc_ms(2026, 7, 8, 12)) == timedelta(hours=3)
    assert NYC.offset_at_utc(utc_ms(2026, 1, 7, 12)) == timedelta(hours=2)
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


def opens_for(true_model, start, end, *, drop_after_friday_ny=None):
    utc = utc_bar_opens(start, end, M1)
    out = [server_naive(t, true_model) for t in utc]
    if drop_after_friday_ny is not None:  # simulate a broker that stops gold early on Fridays (server time)
        out = [o for o in out if not (o.weekday() == 4 and o.time() >= drop_after_friday_ny)
               and not (o.weekday() == 5)]
    return out


SUMMER = (utc_ms(2026, 7, 9, 12), utc_ms(2026, 7, 13, 12))
WINTER = (utc_ms(2026, 1, 8, 12), utc_ms(2026, 1, 12, 12))


@pytest.mark.parametrize("week", [SUMMER, WINTER])
def test_correct_model_is_consistent_on_both_sides(week):
    report = verify_server_time(opens_for(NYC, *week), M1, NYC)
    assert report.status == "consistent" and report.problems() == []
    assert {c.side for c in report.checks} == {"friday_close", "sunday_open"}
    assert all(c.verdict is Verdict.CONSISTENT for c in report.checks)
    assert all(w.verified for w in report.weeks)


@pytest.mark.parametrize("declared", ["fixed:+02:00", "fixed:+00:00", "Europe/London"])
def test_wrong_model_is_contradicted(declared):
    report = verify_server_time(opens_for(NYC, *SUMMER), M1, ServerTimeModel(declared))
    assert report.status == "contradicted" and report.problems()


def test_review_finding_1_trailing_week_after_dst_change_is_checked():
    # Broker uses ny_close_midnight; export Mar 2-11 2026 spans the US DST change (Sun 8 Mar). Declared
    # Europe/Athens agrees before 8 Mar but is wrong by 1 h afterwards. The Sunday-reopen check catches it.
    opens = opens_for(NYC, utc_ms(2026, 3, 2), utc_ms(2026, 3, 11, 12))
    report = verify_server_time(opens, M1, ServerTimeModel("Europe/Athens"))
    assert report.status == "contradicted"
    assert [c.side for c in report.contradictions] == ["sunday_open"]


def test_review_finding_2_early_friday_close_cannot_validate_a_wrong_model():
    # Broker really is ny_close_midnight (UTC+3 in July) but stops gold at 23:00 server time on Friday.
    opens = opens_for(NYC, *SUMMER, drop_after_friday_ny=time(23, 0))
    # A wrong +02:00 model matches the early close but is contradicted by the Sunday reopen -> refused.
    assert verify_server_time(opens, M1, ServerTimeModel("fixed:+02:00")).status == "contradicted"
    # The right model with default anchors is also refused (close side off by 1 h): fail closed...
    assert verify_server_time(opens, M1, NYC).status == "contradicted"
    # ...until the broker's real Friday close (16:00 New York) is configured.
    anchors = SessionAnchors(friday_close_ny=time(16, 0))
    assert verify_server_time(opens, M1, NYC, anchors=anchors).status == "consistent"


def test_review_finding_4_holiday_early_close_is_anomalous_not_contradiction():
    # Close at 13:45 New York on Fri 27 Nov 2026 (day after Thanksgiving): delta -3h15m -> anomalous, not a
    # contradiction. Every week stays verified through its other boundary check.
    opens = opens_for(NYC, utc_ms(2026, 11, 19, 12), utc_ms(2026, 12, 1, 12))
    cutoff = server_naive(utc_ms(2026, 11, 27, 18, 45), NYC)  # 13:45 EST = 18:45 UTC
    opens = [o for o in opens if not (cutoff <= o < datetime(2026, 11, 29))]
    report = verify_server_time(opens, M1, NYC)
    verdicts = {c.side: c.verdict for c in report.checks if c.expected_utc.day == 27}
    assert verdicts == {"friday_close": Verdict.ANOMALOUS}
    assert report.status == "consistent" and all(w.verified for w in report.weeks)


def test_week_with_only_anomalous_evidence_is_unverified():
    # An export that starts at a Sunday reopen and whose only boundary is a holiday early close has no
    # usable evidence for that week: its bars cannot be placed in UTC.
    opens = opens_for(NYC, utc_ms(2026, 11, 22, 23), utc_ms(2026, 11, 27, 18, 45))
    opens += opens_for(NYC, utc_ms(2026, 11, 29, 23), utc_ms(2026, 11, 30, 2))
    report = verify_server_time(opens, M1, NYC)
    assert [w.verified for w in report.weeks] == [False, True]


def test_export_without_weekend_has_no_evidence():
    opens = opens_for(NYC, utc_ms(2026, 7, 7), utc_ms(2026, 7, 8))
    report = verify_server_time(opens, M1, NYC)
    assert report.status == "no_evidence" and "no week boundary" in report.problems()[0]


def test_good_friday_closure_thursday_close_is_anomalous_reopen_still_verifies():
    # Gold closed on Good Friday (3 Apr 2026): the last bar is Thursday, so the close side is anomalous,
    # while the normal Sunday 18:00 New York reopen (= Monday 01:00 server time) is consistent.
    opens = [datetime(2026, 4, 2, 20, 0) + timedelta(minutes=i) for i in range(30)]   # Thursday, server time
    opens += [datetime(2026, 4, 6, 1, 0) + timedelta(minutes=i) for i in range(30)]   # Monday 01:00 server
    report = verify_server_time(opens, M1, NYC)
    assert {c.side: c.verdict for c in report.checks} == {"friday_close": Verdict.ANOMALOUS,
                                                          "sunday_open": Verdict.CONSISTENT}
    assert [w.verified for w in report.weeks] == [False, True]  # Thursday's week has no usable evidence
