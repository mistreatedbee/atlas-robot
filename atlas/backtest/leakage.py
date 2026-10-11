"""R10 leakage test: a strategy+engine run must not change its past when the future changes or disappears."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import replace

from ..models import Candle
from .engine import BacktestResult, Strategy, run_backtest


DECISION_KINDS = {"decision"}  # the strategy's raw output; its disposition may legitimately differ at the cut


def _records(result: BacktestResult, cutoff: int) -> list:
    """Records knowable from data up to the cut bar. Decisions made AT the cut bar's close (time == cutoff) are
    included: they may use only data up to that bar, so a one-bar peek ahead shows up as a mismatch there."""
    trades = [t.record() for t in result.trades if t.exit_time is not None and t.exit_time < cutoff]
    events = [(e.time, e.kind, e.detail) for e in result.events
              if e.time < cutoff or (e.time == cutoff and e.kind in DECISION_KINDS)]
    return [trades, events]


def _scramble(candles: Sequence[Candle], seed: int) -> list[Candle]:
    rng = random.Random(seed)
    out = []
    for c in candles:
        o = c.open * (1 + rng.uniform(-0.01, 0.01))
        cl = o * (1 + rng.uniform(-0.01, 0.01))
        hi, lo = max(o, cl) * (1 + rng.uniform(0, 0.005)), min(o, cl) * (1 - rng.uniform(0, 0.005))
        out.append(replace(c, open=o, high=hi, low=lo, close=cl, bid_open=None, bid_high=None, bid_low=None,
                           bid_close=None, ask_open=None, ask_high=None, ask_low=None, ask_close=None))
    return out


def check_no_lookahead(make_strategy: Callable[[], Strategy], candles: Sequence[Candle], cut_indices: Sequence[int],
                       **run_kwargs) -> list[str]:
    """Return mismatch descriptions (empty = no leakage detected) for every cut point."""
    full = run_backtest(candles, make_strategy(), **run_kwargs)
    problems = []
    for k in cut_indices:
        cutoff = candles[k].close_time  # data up to and including bar k
        truncated = run_backtest(candles[:k + 1], make_strategy(), **run_kwargs)
        if _records(truncated, cutoff) != _records(full, cutoff):
            problems.append(f"cut after bar {k}: truncated run differs from full run before {cutoff}")
        future_changed = list(candles[:k + 1]) + _scramble(candles[k + 1:], seed=k)
        altered = run_backtest(future_changed, make_strategy(), **run_kwargs)
        if _records(altered, cutoff) != _records(full, cutoff):
            problems.append(f"cut after bar {k}: randomised future changed records before {cutoff}")
    return problems
