"""Market session calendars, used to tell expected market closures from genuine data gaps.

Only the weekly FX closure is modelled. Public holidays (e.g. 25 Dec, 1 Jan) and
broker-specific maintenance windows are NOT modelled, so they surface as
unexpected gaps for human review rather than being silently excused.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


class SessionCalendar(Protocol):
    name: str

    def bar_in_session(self, open_ms: int, close_ms: int) -> bool:
        """True if any part of [open_ms, close_ms) falls in trading hours."""


class AlwaysOpen:
    """Crypto spot venues trade 24/7. Exchange outages still appear as gaps."""

    name = "crypto_24x7"

    def bar_in_session(self, open_ms: int, close_ms: int) -> bool:
        return True


class FxWeekly:
    """Spot FX convention: closed from Friday 17:00 to Sunday 17:00 New York time.

    In UTC this is 21:00 (US summer time) or 22:00 (US winter time). Individual
    brokers deviate by minutes; that is recorded as a known limitation.
    """

    name = "fx_24x5"

    @staticmethod
    def _ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def closure_containing(self, ts_ms: int) -> tuple[int, int] | None:
        # Work in naive New York wall-clock time so DST shifts are applied by zoneinfo, not by arithmetic.
        local = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(NEW_YORK).replace(tzinfo=None)
        days_since_friday = (local.weekday() - 4) % 7
        friday_wall = (local - timedelta(days=days_since_friday)).replace(hour=17, minute=0, second=0, microsecond=0)
        if self._ms(friday_wall.replace(tzinfo=NEW_YORK)) > ts_ms:
            friday_wall -= timedelta(days=7)
        sunday_wall = friday_wall + timedelta(days=2)
        start = self._ms(friday_wall.replace(tzinfo=NEW_YORK))
        end = self._ms(sunday_wall.replace(tzinfo=NEW_YORK))
        if start <= ts_ms < end:
            return start, end
        return None

    def bar_in_session(self, open_ms: int, close_ms: int) -> bool:
        closure = self.closure_containing(open_ms)
        return not (closure is not None and close_ms <= closure[1])


class XauCfd(FxWeekly):
    """Spot gold (XAU/USD) as typically offered by retail brokers, following the CME metals schedule:

    * weekly closure Friday 17:00 -> Sunday 18:00 New York;
    * daily maintenance break 17:00 -> 18:00 New York, Monday to Thursday.

    Individual brokers differ (break length, Sunday open time, holiday hours). The
    broker's published hours must be checked against observed gaps in its data.
    """

    name = "xau_cfd"

    def closure_containing(self, ts_ms: int) -> tuple[int, int] | None:
        local = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(NEW_YORK).replace(tzinfo=None)
        days_since_friday = (local.weekday() - 4) % 7
        friday_wall = (local - timedelta(days=days_since_friday)).replace(hour=17, minute=0, second=0, microsecond=0)
        if self._ms(friday_wall.replace(tzinfo=NEW_YORK)) > ts_ms:
            friday_wall -= timedelta(days=7)
        weekly = (friday_wall, friday_wall + timedelta(days=2, hours=1))
        daily_start = local.replace(hour=17, minute=0, second=0, microsecond=0)
        daily = (daily_start, daily_start + timedelta(hours=1)) if local.weekday() <= 3 else None
        for window in (weekly, daily):
            if window is None:
                continue
            start = self._ms(window[0].replace(tzinfo=NEW_YORK))
            end = self._ms(window[1].replace(tzinfo=NEW_YORK))
            if start <= ts_ms < end:
                return start, end
        return None


    def in_reopen_window(self, ts_ms: int, minutes: int) -> bool:
        """True during the first `minutes` after the 18:00 New York reopen (Sunday weekly open, Mon-Thu daily)."""
        local = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(NEW_YORK)
        if local.weekday() not in (6, 0, 1, 2, 3):
            return False
        since = (local.hour - 18) * 60 + local.minute + local.second / 60
        return 0 <= since < minutes


def is_open_at(calendar: SessionCalendar, ts_ms: int) -> bool:
    return calendar.bar_in_session(ts_ms, ts_ms + 1)


def trading_day(ts_ms: int) -> str:
    """Gold trading-day label: New York date of (time + 7 h), so days roll at 17:00 New York (DST-aware)."""
    local = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(NEW_YORK).replace(tzinfo=None)
    return (local + timedelta(hours=7)).date().isoformat()


CALENDARS: dict[str, SessionCalendar] = {c.name: c for c in (AlwaysOpen(), FxWeekly(), XauCfd())}


def get_calendar(name: str) -> SessionCalendar:
    try:
        return CALENDARS[name]
    except KeyError:
        raise ValueError(f"unknown session calendar {name!r}; expected one of {sorted(CALENDARS)}") from None
