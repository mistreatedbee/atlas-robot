"""MT5 server time -> UTC, and verification of the declared model against the data itself.

MT5 exports carry naive timestamps in the broker's *server time*. Supported models:

* ``ny_close_midnight`` — server midnight coincides with 17:00 New York (UTC+2 while the US is on
  standard time, UTC+3 during US daylight time). A common MT5 broker convention; must be verified per broker.
* ``fixed:+HH:MM``      — constant offset from UTC.
* an IANA zone name     — e.g. ``Europe/Athens`` (follows EU daylight-saving dates, which differ from the US).

Verification: the export is split into trading weeks at gaps longer than 24 h. Each week boundary is checked
from BOTH sides — the last bar before the gap against the broker's Friday close, and the first bar after it
against the Sunday reopen (anchors in New York time, configurable because brokers differ; default 17:00 / 18:00).
Each check is CONSISTENT (within tolerance of the declared model), CONTRADICTED (off by a whole 1-3 hours, the typical
mark of a wrong offset/DST rule) or ANOMALOUS (anything else, e.g. a holiday early close; ignored).

* Any contradiction -> the whole import is refused.
* A week with no consistent check -> its bars cannot be placed in UTC and are rejected.
* Data alone cannot rule out a broker whose sessions AND clock are both shifted, so passing these checks makes
  the model "consistent"; it becomes "verified" only with independent evidence (support statement or a
  server-clock-vs-UTC screenshot) recorded in configuration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
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
class SessionAnchors:
    """Broker's weekly gold session boundaries in New York time (confirm from the MT5 'Trades' sessions)."""

    friday_close_ny: time = time(17, 0)
    sunday_open_ny: time = time(18, 0)


class Verdict(StrEnum):
    CONSISTENT = "consistent"
    CONTRADICTED = "contradicted"
    ANOMALOUS = "anomalous"


@dataclass(frozen=True)
class BoundaryCheck:
    side: str  # "friday_close" | "sunday_open"
    observed_server: datetime  # close side: last bar open + timeframe; open side: first bar open
    expected_utc: datetime
    implied_offset: timedelta
    declared_offset: timedelta
    verdict: Verdict

    @property
    def delta_minutes(self) -> float:
        return (self.implied_offset - self.declared_offset).total_seconds() / 60

    def describe(self) -> str:
        return (f"{self.side} near {self.expected_utc:%Y-%m-%d %H:%M} UTC: data implies UTC{_fmt(self.implied_offset)}, "
                f"model gives UTC{_fmt(self.declared_offset)} -> {self.verdict.value}")


@dataclass(frozen=True)
class WeekCheck:
    first_bar_server: datetime
    last_bar_server: datetime
    checks: tuple[BoundaryCheck, ...]

    @property
    def verified(self) -> bool:
        verdicts = {c.verdict for c in self.checks}
        return Verdict.CONSISTENT in verdicts and Verdict.CONTRADICTED not in verdicts


@dataclass(frozen=True)
class VerificationReport:
    model_spec: str
    weeks: tuple[WeekCheck, ...]
    anchors: SessionAnchors = field(default_factory=SessionAnchors)

    @property
    def checks(self) -> list[BoundaryCheck]:
        return [c for w in self.weeks for c in w.checks]

    @property
    def contradictions(self) -> list[BoundaryCheck]:
        return [c for c in self.checks if c.verdict is Verdict.CONTRADICTED]

    @property
    def status(self) -> str:
        if self.contradictions:
            return "contradicted"
        if not any(w.verified for w in self.weeks):
            return "no_evidence"
        return "consistent"

    def problems(self) -> list[str]:
        if self.contradictions:
            return [c.describe() for c in self.contradictions]
        if self.status == "no_evidence":
            return ["no week boundary in the export could be checked; include at least one full weekend "
                    "(Friday close and Sunday reopen) to verify server time"]
        return []

    def week_of(self, server_open: datetime) -> WeekCheck | None:
        for w in self.weeks:
            if w.first_bar_server <= server_open <= w.last_bar_server:
                return w
        return None


def _ny_to_utc(day, at: time) -> datetime:
    local = datetime(day.year, day.month, day.day, at.hour, at.minute, tzinfo=NEW_YORK)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _check(side: str, observed: datetime, weekday: int, at: time, model: ServerTimeModel,
           tolerance: timedelta) -> BoundaryCheck | None:
    days = [observed.date() + timedelta(days=d) for d in (-1, 0, 1)]
    matches = [d for d in days if d.weekday() == weekday]
    if not matches:
        return None  # boundary is not on the expected weekday (e.g. holiday closure): no evidence either way
    expected = _ny_to_utc(matches[0], at)
    implied = observed - expected
    declared = model.offset_at_utc(int(expected.replace(tzinfo=timezone.utc).timestamp() * 1000))
    delta = implied - declared
    if abs(delta) <= tolerance:
        verdict = Verdict.CONSISTENT
    elif any(abs(abs(delta) - timedelta(hours=h)) <= tolerance for h in (1, 2, 3)):
        verdict = Verdict.CONTRADICTED  # whole-hour error: wrong offset or DST rule
    else:
        verdict = Verdict.ANOMALOUS  # early close, late open, holiday: not evidence about the clock
    return BoundaryCheck(side, observed, expected, implied, declared, verdict)


def verify_server_time(server_bar_opens: Sequence[datetime], timeframe_ms: int, model: ServerTimeModel, *,
                       anchors: SessionAnchors = SessionAnchors(), tolerance_minutes: float = 10) -> VerificationReport:
    opens = sorted(server_bar_opens)
    if not opens:
        return VerificationReport(model.spec, (), anchors)
    tf = timedelta(milliseconds=timeframe_ms)
    tol = timedelta(minutes=tolerance_minutes)
    # split into trading weeks at long gaps
    bounds = [0] + [i for i in range(1, len(opens)) if (opens[i] - opens[i - 1]) > timedelta(milliseconds=WEEKEND_GAP_MS)]
    bounds.append(len(opens))
    weeks = []
    n_weeks = len(bounds) - 1
    for w in range(n_weeks):
        first, last = opens[bounds[w]], opens[bounds[w + 1] - 1]
        checks = []
        if w > 0:  # the gap before this week: Sunday reopen
            c = _check("sunday_open", first, 6, anchors.sunday_open_ny, model, tol)
            if c is not None:
                checks.append(c)
        if w < n_weeks - 1:  # the gap after this week: Friday close
            c = _check("friday_close", last + tf, 4, anchors.friday_close_ny, model, tol)
            if c is not None:
                checks.append(c)
        weeks.append(WeekCheck(first, last, tuple(checks)))
    return VerificationReport(model.spec, tuple(weeks), anchors)


def _fmt(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
