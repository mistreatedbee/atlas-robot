"""MT5 server time -> UTC, and verification of the declared model against the data itself.

MT5 exports carry naive timestamps in the broker's *server time*. Supported models:

* ``ny_close_midnight`` — server midnight coincides with 17:00 New York (UTC+2 while the US is on
  standard time, UTC+3 during US daylight time). A common MT5 broker convention; must be verified per broker.
* ``fixed:+HH:MM``      — constant offset from UTC.
* an IANA zone name     — e.g. ``Europe/Athens`` (follows EU daylight-saving dates, which differ from the US).

Inference: for gold the weekly close is Friday 17:00 New York (calendar ``xau_cfd``). The last bar before each
weekend gap therefore reveals the server's UTC offset at that moment. A declared model is accepted only if it
matches every usable weekend in the export.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..sessions import NEW_YORK

HOUR_MS = 3_600_000
WEEKEND_GAP_MS = 24 * HOUR_MS  # any gap longer than this is treated as a weekend/holiday closure


class ServerTimeError(ValueError):
    pass


class ServerTimeModel:
    def __init__(self, spec: str):
        self.spec = spec
        self._fixed: timedelta | None = None
        self._zone: ZoneInfo | None = None
        if spec == "ny_close_midnight":
            pass
        elif m := re.fullmatch(r"fixed:([+-])(\d{2}):(\d{2})", spec):
            sign = 1 if m.group(1) == "+" else -1
            self._fixed = sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        else:
            try:
                self._zone = ZoneInfo(spec)
            except (ZoneInfoNotFoundError, ValueError):
                raise ServerTimeError(f"unknown server-time model {spec!r}") from None

    def offset_at_utc(self, utc_ms: int) -> timedelta:
        """Server UTC offset in force at a UTC instant."""
        if self._fixed is not None:
            return self._fixed
        dt = datetime.fromtimestamp(utc_ms / 1000, timezone.utc)
        if self._zone is not None:
            return dt.astimezone(self._zone).utcoffset()
        return dt.astimezone(NEW_YORK).utcoffset() + timedelta(hours=7)  # NY 17:00 == server 00:00

    def to_utc_ms(self, server_naive: datetime) -> int:
        """Convert a naive server timestamp to UTC ms. Offsets change only inside weekend closures for gold,
        so a fixed-point iteration converges; a non-converging result means the time does not exist."""
        guess = int(server_naive.replace(tzinfo=timezone.utc).timestamp() * 1000)
        utc = guess
        for _ in range(3):
            utc = guess - int(self.offset_at_utc(utc).total_seconds() * 1000)
        if guess - int(self.offset_at_utc(utc).total_seconds() * 1000) != utc:
            raise ServerTimeError(f"server time {server_naive} is ambiguous or non-existent under {self.spec}")
        return utc


@dataclass(frozen=True)
class WeekendObservation:
    last_bar_server: datetime  # naive server time of the last bar's OPEN before the gap
    server_close: datetime  # last bar open + timeframe
    expected_close_utc: datetime  # Friday 17:00 New York, in UTC
    inferred_offset: timedelta | None  # None when the weekend is unusable (e.g. holiday early close)
    residual_minutes: float


@dataclass(frozen=True)
class InferenceReport:
    observations: tuple[WeekendObservation, ...]

    @property
    def usable(self) -> list[WeekendObservation]:
        return [o for o in self.observations if o.inferred_offset is not None]

    def offsets(self) -> set[timedelta]:
        return {o.inferred_offset for o in self.usable}


def _friday_close_utc(friday_date) -> datetime:
    local = datetime(friday_date.year, friday_date.month, friday_date.day, 17, 0, tzinfo=NEW_YORK)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def infer_offsets(server_bar_opens: Sequence[datetime], timeframe_ms: int, *, tolerance_minutes: float = 20) -> InferenceReport:
    """Infer the server UTC offset from each weekend gap in naive server-time bar opens (sorted or not)."""
    opens = sorted(server_bar_opens)
    tf = timedelta(milliseconds=timeframe_ms)
    obs = []
    for prev, cur in zip(opens, opens[1:]):
        if (cur - prev).total_seconds() * 1000 <= WEEKEND_GAP_MS:
            continue
        server_close = prev + tf
        # The true offset is within +-14h, so the server-calendar date of the close is within a day of Friday.
        candidates = [server_close.date() + timedelta(days=d) for d in (-1, 0, 1)]
        fridays = [d for d in candidates if d.weekday() == 4]
        if not fridays:
            obs.append(WeekendObservation(prev, server_close, datetime.min, None, float("nan")))
            continue
        expected = _friday_close_utc(fridays[0])
        raw = server_close - expected
        whole = timedelta(minutes=round(raw.total_seconds() / 60 / 30) * 30)  # offsets come in 30-min units
        residual = abs((raw - whole).total_seconds()) / 60
        usable = residual <= tolerance_minutes and timedelta(hours=-12) <= whole <= timedelta(hours=14)
        obs.append(WeekendObservation(prev, server_close, expected, whole if usable else None, residual))
    return InferenceReport(tuple(obs))


def verify_model(model: ServerTimeModel, report: InferenceReport) -> list[str]:
    """Return a list of contradictions between the declared model and the data (empty = consistent)."""
    if not report.usable:
        return ["no usable weekend close in the export; include at least one full Friday close to verify server time"]
    problems = []
    for o in report.usable:
        declared = model.offset_at_utc(int(o.expected_close_utc.replace(tzinfo=timezone.utc).timestamp() * 1000))
        if declared != o.inferred_offset:
            problems.append(
                f"week closing {o.expected_close_utc:%Y-%m-%d}: data implies UTC{_fmt(o.inferred_offset)}, "
                f"model {model.spec} gives UTC{_fmt(declared)}"
            )
    return problems


def _fmt(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
