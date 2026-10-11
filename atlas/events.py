"""Point-in-time scheduled economic events (US tier 1: CPI, Employment Situation, FOMC decisions).

Data: config/events/us_tier1.csv, built by scripts/build_us_events.py from official BLS / Federal Reserve
sources. Every query takes `as_of`: an event is visible only once its `known_since` time has passed, so a backtest
can never react to an event (or a rescheduled date) it could not yet have known about.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EconEvent:
    event_id: str
    category: str
    tier: int
    reference: str
    release_ms: int
    known_since_ms: int
    source: str
    notes: str


class EventCalendar:
    def __init__(self, events: list[EconEvent]):
        self.events = sorted(events, key=lambda e: e.release_ms)
        self._times = [e.release_ms for e in self.events]
        ids = [e.event_id for e in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate event ids")
        for e in self.events:
            if e.known_since_ms > e.release_ms:
                raise ValueError(f"{e.event_id}: known_since after release")

    @classmethod
    def load(cls, path: str | Path) -> EventCalendar:
        with Path(path).open(newline="") as fh:
            return cls([EconEvent(r["event_id"], r["category"], int(r["tier"]), r["reference"],
                                  int(r["release_utc_ms"]), int(r["known_since_utc_ms"]), r["source"], r["notes"])
                        for r in csv.DictReader(fh)])

    def window_event(self, t_ms: int, *, before_ms: int, after_ms: int, as_of_ms: int,
                     max_tier: int = 1) -> EconEvent | None:
        """An event known at `as_of_ms` whose release lies within [t - after, t + before], i.e. `t` is less than
        `before_ms` ahead of it or less than `after_ms` past it. Returns the nearest such event."""
        lo = bisect.bisect_left(self._times, t_ms - after_ms)
        hi = bisect.bisect_right(self._times, t_ms + before_ms)
        candidates = [e for e in self.events[lo:hi] if e.tier <= max_tier and e.known_since_ms <= as_of_ms]
        return min(candidates, key=lambda e: abs(e.release_ms - t_ms)) if candidates else None

    def coverage(self) -> tuple[int, int]:
        return (self._times[0], self._times[-1]) if self._times else (0, 0)
