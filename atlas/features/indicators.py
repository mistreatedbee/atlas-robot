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


def stochastic(high, low, close, n: int = 14, d: int = 3) -> tuple[Array, Array]:
    """%K = 100 (c - lowest low_n) / (highest high_n - lowest low_n); NaN on a flat range. %D = SMA_d(%K)."""
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    _check_period(n)
    k = np.full(c.shape, np.nan)
    if len(c) >= n:
        hh = np.lib.stride_tricks.sliding_window_view(h, n).max(axis=1)
        ll = np.lib.stride_tricks.sliding_window_view(l, n).min(axis=1)
        rng = hh - ll
        with np.errstate(divide="ignore", invalid="ignore"):
            k[n - 1:] = np.where(rng > 0, 100.0 * (c[n - 1:] - ll) / rng, np.nan)
    return k, sma(k, d)


def adx(high, low, close, n: int = 14) -> tuple[Array, Array, Array]:
    """Wilder ADX with +DI/-DI. Smoothing = Wilder average seeded by SMA (pinned; TA-Lib's sum-based seeding
    gives the same DI ratios but a different warm-up). Bar 0 has no previous bar, so all series start at bar 1.
    Returns (adx, plus_di, minus_di)."""
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    _check_period(n)
    up = np.concatenate(([np.nan], np.diff(h)))
    down = np.concatenate(([np.nan], -np.diff(l)))
    plus_dm = np.where((up > down) & (up > 0), up, 0.0) + up * 0  # "+ up*0" keeps the NaN at bar 0
    minus_dm = np.where((down > up) & (down > 0), down, 0.0) + down * 0
    tr = true_range(h, l, c)
    tr[0] = np.nan
    s_tr, s_p, s_m = wilder(tr, n), wilder(plus_dm, n), wilder(minus_dm, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(s_tr > 0, 100.0 * s_p / s_tr, np.nan)
        mdi = np.where(s_tr > 0, 100.0 * s_m / s_tr, np.nan)
        total = pdi + mdi
        dx = np.where(total > 0, 100.0 * np.abs(pdi - mdi) / total, 0.0)
    dx = np.where(np.isnan(pdi) | np.isnan(mdi), np.nan, dx)
    return wilder(dx, n), pdi, mdi


def confirmed_swings(high, low, k: int = 3) -> tuple[Array, Array]:
    """Market-structure swings, reported only once CONFIRMED.

    A swing high at bar j: high[j] > every high in the k bars before it and >= every high in the k bars after it.
    It is only known at bar j + k, so output[t] = price of the most recent swing high confirmed by bar t.
    Swing lows are the mirror image. Values are NaN until the first confirmation.
    """
    h, l = _as_float(high), _as_float(low)
    _check_period(k)
    last_hi = np.full(h.shape, np.nan)
    last_lo = np.full(l.shape, np.nan)
    cur_hi = cur_lo = np.nan
    for t in range(len(h)):
        j = t - k  # candidate whose right-hand window is complete at t
        if j - k >= 0:
            left_h, right_h = h[j - k:j], h[j + 1:t + 1]
            if h[j] > left_h.max() and h[j] >= right_h.max():
                cur_hi = h[j]
            left_l, right_l = l[j - k:j], l[j + 1:t + 1]
            if l[j] < left_l.min() and l[j] <= right_l.min():
                cur_lo = l[j]
        last_hi[t], last_lo[t] = cur_hi, cur_lo
    return last_hi, last_lo
