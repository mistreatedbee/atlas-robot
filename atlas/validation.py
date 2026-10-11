"""Candle and series validation rules.

Errors make a candle INVALID: it is still stored for audit but excluded from
research queries by default. Warnings are recorded but do not exclude data.
Missing bars are never filled in; they are reported as gaps.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from .models import Candle, PriceBasis, ValidationStatus, VolumeKind, timeframe_ms
from .sessions import SessionCalendar


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    open_time: int | None
    message: str


@dataclass(frozen=True, slots=True)
class Gap:
    """Bars missing between two stored candles. `start`/`end` bound the missing interval [start, end)."""

    start: int
    end: int
    missing_bars: int
    expected_closure: bool  # True if every missing bar falls inside a known market closure


@dataclass(slots=True)
class SeriesReport:
    candle_count: int
    first_open_time: int | None
    last_close_time: int | None
    issues: list[Issue] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    @property
    def unexpected_gaps(self) -> list[Gap]:
        return [g for g in self.gaps if not g.expected_closure]

    @property
    def status(self) -> ValidationStatus:
        if any(i.severity is Severity.ERROR for i in self.issues):
            return ValidationStatus.INVALID
        if self.issues or self.unexpected_gaps:
            return ValidationStatus.WARNING
        return ValidationStatus.VALID

    def summary(self) -> dict:
        codes: dict[str, int] = {}
        for issue in self.issues:
            codes[issue.code] = codes.get(issue.code, 0) + 1
        return {
            "status": self.status.value,
            "candle_count": self.candle_count,
            "first_open_time": self.first_open_time,
            "last_close_time": self.last_close_time,
            "issue_counts": codes,
            "issues": [asdict(i) for i in self.issues],
            "gaps": [asdict(g) for g in self.gaps],
            "unexpected_gap_count": len(self.unexpected_gaps),
            "unexpected_missing_bars": sum(g.missing_bars for g in self.unexpected_gaps),
            "expected_closure_count": sum(1 for g in self.gaps if g.expected_closure),
        }


def _err(code: str, c: Candle, msg: str) -> Issue:
    return Issue(code, Severity.ERROR, c.open_time, msg)


def validate_candle(c: Candle, now_ms: int) -> list[Issue]:
    """Rules that can be checked on a single candle in isolation."""
    issues: list[Issue] = []
    tf = timeframe_ms(c.timeframe)

    prices = {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
    optional = {
        name: getattr(c, name)
        for name in ("bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close")
        if getattr(c, name) is not None
    }
    for name, value in {**prices, **optional}.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            issues.append(_err("NON_FINITE_PRICE", c, f"{name}={value!r}"))
        elif value <= 0:
            issues.append(_err("NON_POSITIVE_PRICE", c, f"{name}={value}"))
    if issues:
        return issues  # relational checks are meaningless on bad numbers

    if c.high < c.low:
        issues.append(_err("HIGH_BELOW_LOW", c, f"high={c.high} < low={c.low}"))
    if c.high < max(c.open, c.close):
        issues.append(_err("HIGH_BELOW_BODY", c, f"high={c.high} < max(open,close)={max(c.open, c.close)}"))
    if c.low > min(c.open, c.close):
        issues.append(_err("LOW_ABOVE_BODY", c, f"low={c.low} > min(open,close)={min(c.open, c.close)}"))

    for field_name in ("open", "high", "low", "close"):
        bid = getattr(c, f"bid_{field_name}")
        ask = getattr(c, f"ask_{field_name}")
        if bid is not None and ask is not None and bid > ask:
            issues.append(_err("CROSSED_QUOTE", c, f"bid_{field_name}={bid} > ask_{field_name}={ask}"))
    if c.price_basis is PriceBasis.BID and c.bid_close is not None and c.bid_close != c.close:
        issues.append(_err("BASIS_MISMATCH", c, "price_basis=bid but close != bid_close"))

    if c.volume is not None:
        if not math.isfinite(c.volume) or c.volume < 0:
            issues.append(_err("INVALID_VOLUME", c, f"volume={c.volume}"))
        if c.volume_kind is VolumeKind.NONE:
            issues.append(_err("VOLUME_KIND_MISMATCH", c, "volume present but volume_kind=none"))

    if c.open_time % tf != 0:
        issues.append(_err("MISALIGNED_OPEN_TIME", c, f"open_time not aligned to {c.timeframe}"))
    if c.close_time != c.open_time + tf:
        issues.append(_err("BAD_CLOSE_TIME", c, f"close_time - open_time = {c.close_time - c.open_time} ms, expected {tf}"))
    if c.close_time > now_ms:
        issues.append(_err("UNFINISHED_CANDLE", c, f"close_time {c.close_time} is after evaluation time {now_ms}"))
    return issues


def status_for(issues: Sequence[Issue]) -> ValidationStatus:
    if any(i.severity is Severity.ERROR for i in issues):
        return ValidationStatus.INVALID
    return ValidationStatus.WARNING if issues else ValidationStatus.VALID


def validate_series(
    candles: Sequence[Candle],
    calendar: SessionCalendar,
    now_ms: int,
    *,
    max_staleness_ms: int | None = None,
    spike_factor: float = 20.0,
    spike_min_bars: int = 50,
    flat_run_bars: int = 10,
) -> SeriesReport:
    """Rules that need neighbouring candles: duplicates, ordering, gaps, spikes, stale feeds."""
    report = SeriesReport(
        candle_count=len(candles),
        first_open_time=min((c.open_time for c in candles), default=None),
        last_close_time=max((c.close_time for c in candles), default=None),
    )
    if not candles:
        report.issues.append(Issue("EMPTY_SERIES", Severity.WARNING, None, "no candles in range"))
        return report

    keys = {(c.instrument_id, c.provider, c.venue, c.timeframe) for c in candles}
    if len(keys) > 1:
        raise ValueError(f"validate_series expects a single series, got {sorted(keys)}")
    tf = timeframe_ms(candles[0].timeframe)

    for prev, cur in zip(candles, candles[1:]):
        if cur.open_time < prev.open_time:
            report.issues.append(Issue("OUT_OF_ORDER", Severity.WARNING, cur.open_time, "candles not sorted by open_time"))
            break

    ordered = sorted(candles, key=lambda c: c.open_time)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.open_time == prev.open_time:
            sev = Severity.WARNING if cur.values_key() == prev.values_key() else Severity.ERROR
            report.issues.append(Issue("DUPLICATE_OPEN_TIME", sev, cur.open_time, "same open_time appears twice"))
            continue
        if cur.open_time > prev.open_time + tf:
            missing = list(range(prev.open_time + tf, cur.open_time, tf))
            expected = all(not calendar.bar_in_session(t, t + tf) for t in missing)
            report.gaps.append(Gap(missing[0], cur.open_time, len(missing), expected))

    for c in ordered:
        if not calendar.bar_in_session(c.open_time, c.close_time):
            report.issues.append(Issue("BAR_DURING_CLOSURE", Severity.WARNING, c.open_time, "bar inside a market closure"))

    if len(ordered) >= spike_min_bars:
        ranges = [c.high - c.low for c in ordered]
        median_range = statistics.median(ranges)
        if median_range > 0:
            for c, r in zip(ordered, ranges):
                if r > spike_factor * median_range:
                    report.issues.append(Issue(
                        "PRICE_SPIKE", Severity.WARNING, c.open_time,
                        f"range {r:.10g} is {r / median_range:.1f}x the median range",
                    ))

    run_start, run_len = None, 0
    for prev, cur in zip(ordered, ordered[1:] + [None]):
        flat = prev.open == prev.high == prev.low == prev.close
        if flat:
            run_start = prev.open_time if run_len == 0 else run_start
            run_len += 1
        if not flat or cur is None:
            if run_len >= flat_run_bars:
                report.issues.append(Issue(
                    "FLAT_RUN", Severity.WARNING, run_start, f"{run_len} consecutive bars with open=high=low=close",
                ))
            run_len = 0

    if max_staleness_ms is not None and report.last_close_time is not None:
        age = now_ms - report.last_close_time
        if age > max_staleness_ms:
            report.issues.append(Issue(
                "STALE_DATA", Severity.WARNING, None, f"latest close is {age / 60_000:.1f} min old",
            ))
    return report
