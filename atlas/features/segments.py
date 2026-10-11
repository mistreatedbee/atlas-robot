"""Contiguous-segment handling so indicators never bridge missing data.

Policy (declared per experiment):
* ``break_on_unexpected_gap`` (default): bars on either side of a *market closure* (weekend, daily break) are
  treated as consecutive, as a trader would see them; any other missing bar starts a new segment.
* ``break_on_any_gap``: every missing bar, including closures, starts a new segment.
Missing bars are never filled.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum

import numpy as np

from ..models import Candle, timeframe_ms
from ..sessions import SessionCalendar


class GapPolicy(StrEnum):
    BREAK_ON_UNEXPECTED_GAP = "break_on_unexpected_gap"
    BREAK_ON_ANY_GAP = "break_on_any_gap"


def segment_ids(candles: Sequence[Candle], calendar: SessionCalendar, policy: GapPolicy) -> np.ndarray:
    """One integer per candle; a new id starts after every gap that the policy treats as a break."""
    if not candles:
        return np.zeros(0, dtype=int)
    tf = timeframe_ms(candles[0].timeframe)
    ids = np.zeros(len(candles), dtype=int)
    current = 0
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1].open_time, candles[i].open_time
        if cur <= prev:
            raise ValueError("candles must be strictly increasing in open_time")
        if cur != prev + tf:
            missing = range(prev + tf, cur, tf)
            closure_only = all(not calendar.bar_in_session(t, t + tf) for t in missing)
            if policy is GapPolicy.BREAK_ON_ANY_GAP or not closure_only:
                current += 1
        ids[i] = current
    return ids


def per_segment(fn: Callable[..., np.ndarray], ids: np.ndarray, *arrays: np.ndarray, **kwargs) -> np.ndarray:
    """Apply a single-output indicator separately to each segment and stitch the results."""
    out = np.full(len(ids), np.nan)
    for seg in np.unique(ids):
        mask = ids == seg
        out[mask] = fn(*(a[mask] for a in arrays), **kwargs)
    return out
