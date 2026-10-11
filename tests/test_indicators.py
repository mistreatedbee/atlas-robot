from __future__ import annotations

import math
import random
import statistics

import numpy as np
import pytest

from atlas.features.indicators import (
    adx, atr, bollinger, confirmed_swings, ema, macd, rsi, sma, stochastic, true_range, wilder,
)
from atlas.features.segments import GapPolicy, per_segment, segment_ids
from atlas.sessions import XauCfd

from .conftest import make_candle, utc_ms

nan = math.nan


def same(a, b):
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=0, atol=1e-10)


# ---- hand-computed values ------------------------------------------------------------------------

def test_sma_hand_values():
    assert same(sma([1, 2, 3, 4, 5], 3), [nan, nan, 2, 3, 4])


def test_ema_seeded_with_sma():
    # seed = mean(1,2,3) = 2 ; alpha = 0.5 -> 3, 4, 5
    assert same(ema([1, 2, 3, 4, 5, 6], 3), [nan, nan, 2, 3, 4, 5])


def test_wilder_hand_values():
    # seed 2 ; alpha 1/3: 4/3 + 2*2/3 = 8/3 ; 5/3 + (8/3)*(2/3) = 31/9
    assert same(wilder([1, 2, 3, 4, 5], 3), [nan, nan, 2, 8 / 3, 31 / 9])


def test_true_range_uses_previous_close():
    h, l, c = [10, 12, 11], [9, 10, 8], [9.5, 11, 9]
    # bar0: 10-9=1 ; bar1: max(2, |12-9.5|=2.5, |10-9.5|=.5)=2.5 ; bar2: max(3, |11-11|=0, |8-11|=3)=3
    assert same(true_range(h, l, c), [1, 2.5, 3])
    assert same(atr(h, l, c, 2), [nan, 1.75, 2.375])


def test_rsi_extremes_and_flat():
    assert same(rsi(list(range(1, 20)), 14)[15:], [100.0] * 4)
    assert same(rsi(list(range(20, 1, -1)), 14)[15:], [0.0] * 4)
    assert same(rsi([5.0] * 20, 14)[15:], [50.0] * 5)


def test_bollinger_population_std():
    x = [1.0, 2.0, 4.0, 7.0]
    lo, mid, hi = bollinger(x, 4, 2.0)
    sd = statistics.pstdev(x)
    assert same([lo[-1], mid[-1], hi[-1]], [3.5 - 2 * sd, 3.5, 3.5 + 2 * sd])
    assert np.isnan(mid[2])


def test_macd_is_difference_of_emas():
    x = np.cumsum(np.random.default_rng(1).normal(size=200)) + 2400
    line, sig, hist = macd(x)
    assert same(line, ema(x, 12) - ema(x, 26))
    assert same(sig, ema(line, 9))
    assert same(hist, line - sig)
    assert np.isnan(sig[:33]).all() and not np.isnan(sig[33])  # warm-up = 26 + 9 - 2 bars


# ---- independent reference implementations (deliberately naive loops) ---------------------------

def ref_rsi(close, n):
    out = [nan] * len(close)
    gains = [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(close)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ref_atr(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(h))]
    out = [nan] * len(h)
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(h)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


@pytest.fixture(scope="module")
def walk():
    rng = random.Random(7)
    c = [2400.0]
    for _ in range(499):
        c.append(c[-1] + rng.gauss(0, 0.5))
    h = [x + abs(rng.gauss(0, 0.3)) for x in c]
    l = [x - abs(rng.gauss(0, 0.3)) for x in c]
    return np.array(h), np.array(l), np.array(c)


def test_rsi_matches_reference(walk):
    _, _, c = walk
    assert same(rsi(c, 14), ref_rsi(list(c), 14))


def test_atr_matches_reference(walk):
    h, l, c = walk
    assert same(atr(h, l, c, 14), ref_atr(list(h), list(l), list(c), 14))


# ---- causality: no look-ahead ---------------------------------------------------------------------

INDICATORS = {
    "sma": lambda h, l, c: sma(c, 20),
    "ema": lambda h, l, c: ema(c, 20),
    "rsi": lambda h, l, c: rsi(c, 14),
    "atr": lambda h, l, c: atr(h, l, c, 14),
    "bb_upper": lambda h, l, c: bollinger(c, 20)[2],
    "macd_signal": lambda h, l, c: macd(c)[1],
    "stoch_d": lambda h, l, c: stochastic(h, l, c, 14, 3)[1],
    "adx": lambda h, l, c: adx(h, l, c, 14)[0],
    "minus_di": lambda h, l, c: adx(h, l, c, 14)[2],
    "swing_high": lambda h, l, c: confirmed_swings(h, l, 3)[0],
    "swing_low": lambda h, l, c: confirmed_swings(h, l, 3)[1],
}


@pytest.mark.parametrize("name", INDICATORS)
def test_no_look_ahead(walk, name):
    h, l, c = walk
    full = INDICATORS[name](h, l, c)
    for t in (0, 1, 13, 14, 33, 34, 100, 257, 499):
        truncated = INDICATORS[name](h[:t + 1], l[:t + 1], c[:t + 1])
        assert same(truncated, full[:t + 1]), f"{name} changed at t<={t} when future bars were removed"


def test_nan_restarts_warm_up_instead_of_bridging():
    x = [1, 2, 3, nan, 5, 6, 7, 8]
    assert same(sma(x, 3), [nan, nan, 2, nan, nan, nan, 6, 7])
    assert same(ema(x, 3), [nan, nan, 2, nan, nan, nan, 6, 7])


@pytest.mark.parametrize("bad", [0, -1, 2.5])
def test_invalid_periods(bad):
    with pytest.raises(ValueError):
        sma([1, 2, 3], bad)


# ---- segments ------------------------------------------------------------------------------------

def bars(times):
    return [make_candle(t, tf="M5", instrument_id="XAUUSD") for t in times]


M5 = 300_000


def test_daily_break_is_not_a_segment_break_by_default():
    # Tue 14 Jul 2026: last bar 20:55 UTC, gold daily break 21:00-22:00 UTC, next bar 22:00
    before = [utc_ms(2026, 7, 14, 20, 50), utc_ms(2026, 7, 14, 20, 55)]
    after = [utc_ms(2026, 7, 14, 22, 0), utc_ms(2026, 7, 14, 22, 5)]
    candles = bars(before + after)
    assert list(segment_ids(candles, XauCfd(), GapPolicy.BREAK_ON_UNEXPECTED_GAP)) == [0, 0, 0, 0]
    assert list(segment_ids(candles, XauCfd(), GapPolicy.BREAK_ON_ANY_GAP)) == [0, 0, 1, 1]


def test_unexpected_gap_starts_new_segment_and_indicators_restart():
    t = [utc_ms(2026, 7, 14, 10, 0) + i * M5 for i in (0, 1, 2, 3, 6, 7, 8, 9)]  # bars 4-5 missing
    candles = bars(t)
    ids = segment_ids(candles, XauCfd(), GapPolicy.BREAK_ON_UNEXPECTED_GAP)
    assert list(ids) == [0, 0, 0, 0, 1, 1, 1, 1]
    close = np.arange(1.0, 9.0)
    assert same(per_segment(sma, ids, close, n=3), [nan, nan, 2, 3, nan, nan, 6, 7])


def test_segments_require_increasing_times():
    with pytest.raises(ValueError):
        segment_ids(bars([utc_ms(2026, 7, 14, 10), utc_ms(2026, 7, 14, 10)]), XauCfd(), GapPolicy.BREAK_ON_ANY_GAP)


# ---- stochastic / ADX / swings -----------------------------------------------------------------------

def test_stochastic_hand_values():
    h, l, c = [10, 11, 12, 13], [8, 9, 10, 9], [9, 10, 11, 12]
    k, d = stochastic(h, l, c, n=3, d=2)
    # bar2: HH=12 LL=8 -> 100*(11-8)/4 = 75 ; bar3: HH=13 LL=9 -> 100*(12-9)/4 = 75
    assert same(k, [nan, nan, 75, 75]) and same(d, [nan, nan, nan, 75])
    flat_k, _ = stochastic([5] * 4, [5] * 4, [5] * 4, n=3)
    assert np.isnan(flat_k).all()


def ref_adx(h, l, c, n):
    """Independent naive ADX with the same pinned conventions."""
    pdm, mdm, tr = [nan], [nan], [nan]
    for i in range(1, len(h)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))

    def wild(x):
        out, acc = [nan] * len(x), None
        vals = [(i, v) for i, v in enumerate(x) if not math.isnan(v)]
        for count, (i, v) in enumerate(vals, start=1):
            if count < n:
                continue
            acc = sum(v2 for _, v2 in vals[:n]) / n if count == n else (acc * (n - 1) + v) / n
            out[i] = acc
        return out
    st, sp, sm = wild(tr), wild(pdm), wild(mdm)
    dx = []
    for a, b, t in zip(sp, sm, st):
        if math.isnan(t):
            dx.append(nan)
            continue
        pdi, mdi = 100 * a / t, 100 * b / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0)
    return wild(dx)


def test_adx_matches_reference(walk):
    h, l, c = walk
    assert same(adx(h, l, c, 14)[0], ref_adx(list(h), list(l), list(c), 14))
    assert np.isnan(adx(h, l, c, 14)[0][:27]).all() and not np.isnan(adx(h, l, c, 14)[0][27])


def test_adx_strong_trend_is_high():
    h = np.arange(100.0, 160.0)
    a, pdi, mdi = adx(h + 0.5, h - 0.5, h, 14)
    assert a[-1] > 90 and pdi[-1] > mdi[-1]


def test_swings_are_reported_only_after_confirmation():
    high = [1, 2, 5, 2, 1, 1, 1]
    low = [1, 1, 1, 1, 0, 1, 1]
    hi, lo = confirmed_swings(high, low, k=2)
    # swing high 5 at bar 2 is confirmed at bar 4 (2 bars later), never before
    assert same(hi, [nan, nan, nan, nan, 5, 5, 5])
    # swing low 0 at bar 4 is confirmed at bar 6
    assert same(lo, [nan, nan, nan, nan, nan, nan, 0])
