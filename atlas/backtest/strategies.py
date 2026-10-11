"""Baseline strategies (docs/13 R9). Baselines exist to be beaten after costs; they are not trading ideas."""

from __future__ import annotations

import random
from collections.abc import Sequence

from ..models import Candle
from ..sessions import XauCfd, trading_day
from .engine import Signal


class NoTrade:
    """R9.1: never trades. Every strategy must beat this after costs."""

    name = "no_trade"
    hypothesis_id = "BASELINE-no_trade"

    def params(self) -> dict:
        return {}

    def prepare(self, candles: Sequence[Candle]) -> None:
        pass

    def decide(self, i: int) -> Signal | None:
        return None


class RandomEntry:
    """R9.2: random side, entry probability per UTC hour (matched to a strategy, or uniform).

    Each bar's draw is derived from (seed, bar open time) only, so results do not depend on how much data follows
    (required by the R10 leakage test) and are exactly reproducible.
    """

    name = "random_entry"

    def __init__(self, *, seed: int, stop_usd: float, tp_usd: float | None, p: float | None = None,
                 p_by_hour: dict[int, float] | None = None, hypothesis_id: str = "BASELINE-random_entry"):
        if (p is None) == (p_by_hour is None):
            raise ValueError("give exactly one of p or p_by_hour")
        probs = [p] if p is not None else list(p_by_hour.values())
        if any(not 0 <= x <= 1 for x in probs):
            raise ValueError("probabilities must be in [0, 1]")
        self.seed, self.stop_usd, self.tp_usd = seed, stop_usd, tp_usd
        self.p, self.p_by_hour = p, dict(p_by_hour or {})
        self.hypothesis_id = hypothesis_id
        self._candles: Sequence[Candle] = ()

    def params(self) -> dict:
        return {"seed": self.seed, "stop_usd": self.stop_usd, "tp_usd": self.tp_usd, "p": self.p,
                "p_by_hour": {str(k): v for k, v in sorted(self.p_by_hour.items())}}

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles

    def decide(self, i: int) -> Signal | None:
        c = self._candles[i]
        rng = random.Random(f"{self.seed}:{c.open_time}")
        p = self.p if self.p is not None else self.p_by_hour.get((c.close_time // 3_600_000) % 24, 0.0)
        if rng.random() >= p:
            return None
        return Signal("buy" if rng.random() < 0.5 else "sell", self.stop_usd, self.tp_usd)


class SessionLong:
    """R9.3: one long per trading day at the first decision after the reopen window; catastrophic stop."""

    name = "session_long"
    hypothesis_id = "BASELINE-session_long"

    def __init__(self, *, stop_usd: float, reopen_minutes: int, calendar: XauCfd | None = None):
        self.stop_usd, self.reopen_minutes = stop_usd, reopen_minutes
        self.calendar = calendar or XauCfd()
        self._candles: Sequence[Candle] = ()
        self._entered: set[str] = set()

    def params(self) -> dict:
        return {"stop_usd": self.stop_usd, "reopen_minutes": self.reopen_minutes}

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles
        self._entered = set()

    def decide(self, i: int) -> Signal | None:
        c = self._candles[i]
        day = trading_day(c.open_time)
        # decide only once the NEXT bar (the fill) is past the reopen window
        if day in self._entered or self.calendar.in_reopen_window(c.close_time, self.reopen_minutes):
            return None
        self._entered.add(day)
        return Signal("buy", self.stop_usd, None)


def matched_hourly_probabilities(strategy_trades: Sequence, eligible_bars_by_hour: dict[int, int]) -> dict[int, float]:
    """R9.2: per-UTC-hour entry probability = strategy entries / eligible decision bars in that hour."""
    counts: dict[int, int] = {}
    for t in strategy_trades:
        h = (t.decision_time // 3_600_000) % 24
        counts[h] = counts.get(h, 0) + 1
    return {h: min(1.0, counts.get(h, 0) / n) for h, n in eligible_bars_by_hour.items() if n > 0}
