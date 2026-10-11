"""Chronological train / validation / test periods with embargo (docs/13 R7)."""

from __future__ import annotations

from dataclasses import dataclass

DAY_MS = 86_400_000


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    name: str
    start_ms: int  # inclusive
    end_ms: int  # exclusive


@dataclass(frozen=True)
class Splits:
    train: Period
    validation: Period
    test: Period
    embargo_ms: int
    min_embargo_ms: int = DAY_MS  # caller passes max(1 day, (max_hold_bars + longest lookback) x timeframe)

    def __post_init__(self):
        required = max(DAY_MS, self.min_embargo_ms)
        if self.embargo_ms < required:
            raise SplitError(f"embargo {self.embargo_ms} ms is shorter than required {required} ms "
                             "(max holding period + longest indicator lookback, at least one day)")
        periods = (self.train, self.validation, self.test)
        for p in periods:
            if p.end_ms <= p.start_ms:
                raise SplitError(f"{p.name}: end must be after start")
        for earlier, later in zip(periods, periods[1:]):
            if later.start_ms < earlier.end_ms + self.embargo_ms:
                raise SplitError(f"{later.name} starts before {earlier.name} end + embargo "
                                 f"({self.embargo_ms // DAY_MS} days): periods overlap or are too close")

    def get(self, name: str) -> Period:
        try:
            return {"train": self.train, "validation": self.validation, "test": self.test}[name]
        except KeyError:
            raise SplitError(f"unknown period {name!r}") from None


def required_embargo_ms(max_hold_bars: int, longest_lookback_bars: int, timeframe_ms: int) -> int:
    return max(DAY_MS, (max_hold_bars + longest_lookback_bars) * timeframe_ms)
