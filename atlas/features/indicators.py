"""Core technical indicators on NumPy float arrays (docs/06 definitions).

Conventions (pinned because libraries differ):
* EMA is seeded with the SMA of the first n values; alpha = 2 / (n + 1).
* Wilder smoothing (RSI, ATR) is seeded with the simple mean of the first n values; alpha = 1 / n.
* RSI: avg_loss == 0 and avg_gain > 0 -> 100; both zero (flat market) -> 50.
* Bollinger Bands use the population standard deviation (ddof = 0).
* True range of the first bar is high - low (no previous close).
* Any NaN in the input propagates: values from that point are NaN until a full window of valid data follows
  (callers compute per contiguous segment, see segments.py, rather than bridging gaps).
"""

from __future__ import annotations

import numpy as np

Array = np.ndarray


def _as_float(x) -> Array:
    a = np.asarray(x, dtype=float)
    if a.ndim != 1:
        raise ValueError("indicators expect 1-D arrays")
    return a


def _check_period(n: int) -> None:
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"period must be a positive integer, got {n!r}")


def sma(x, n: int) -> Array:
    x = _as_float(x)
    _check_period(n)
    out = np.full(x.shape, np.nan)
    if len(x) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = windows.mean(axis=1)  # NaN anywhere in the window -> NaN
    return out


def _recursive(x: Array, n: int, alpha: float) -> Array:
    """Exponential smoothing seeded with SMA(n); restarts after NaNs once n valid values accumulate."""
    out = np.full(x.shape, np.nan)
    prev = np.nan
    run: list[float] = []
    for i, v in enumerate(x):
        if np.isnan(v):
            prev, run = np.nan, []
            continue
        if np.isnan(prev):
            run.append(v)
            if len(run) == n:
                prev = sum(run) / n
                out[i] = prev
            continue
        prev = alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def ema(x, n: int) -> Array:
    _check_period(n)
    return _recursive(_as_float(x), n, 2.0 / (n + 1))


def wilder(x, n: int) -> Array:
    _check_period(n)
    return _recursive(_as_float(x), n, 1.0 / n)


def true_range(high, low, close) -> Array:
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    prev_close = np.concatenate(([np.nan], c[:-1]))
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev_close), np.abs(l - prev_close)))
    tr[0] = h[0] - l[0] if len(h) else np.nan
    return tr


def atr(high, low, close, n: int = 14) -> Array:
    return wilder(true_range(high, low, close), n)


def rsi(close, n: int = 14) -> Array:
    c = _as_float(close)
    _check_period(n)
    out = np.full(c.shape, np.nan)
    if len(c) < 2:
        return out
    diff = np.diff(c)
    gain = wilder(np.where(diff > 0, diff, 0.0) + diff * 0, n)  # "+ diff*0" keeps NaN positions
    loss = wilder(np.where(diff < 0, -diff, 0.0) + diff * 0, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
        r = 100.0 - 100.0 / (1.0 + rs)
    r = np.where((loss == 0) & (gain > 0), 100.0, r)
    r = np.where((loss == 0) & (gain == 0), 50.0, r)
    r = np.where(np.isnan(gain) | np.isnan(loss), np.nan, r)
    out[1:] = r
    return out


def bollinger(close, n: int = 20, k: float = 2.0) -> tuple[Array, Array, Array]:
    c = _as_float(close)
    _check_period(n)
    mid = sma(c, n)
    sd = np.full(c.shape, np.nan)
    if len(c) >= n:
        sd[n - 1:] = np.lib.stride_tricks.sliding_window_view(c, n).std(axis=1, ddof=0)
    return mid - k * sd, mid, mid + k * sd


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Array, Array, Array]:
    if not fast < slow:
        raise ValueError("fast period must be shorter than slow period")
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig
