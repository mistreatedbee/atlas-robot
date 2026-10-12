"""Four XAU/USD M1/M5 strategy candidates, one per family in docs/00 (momentum, pullback, range, breakout).

Rules are deliberately simple, explicit and few-parameter (see the matching hypotheses/*.toml). Every feature comes
from atlas.features (causal, look-ahead tested) and is computed per contiguous segment (unexpected gaps restart
indicators). Stops and targets are ATR multiples in US$/oz. decide(i) reads features at index i only.

Live use: the session prepares a strategy once with a list that grows; features are recomputed for the new length
when decide(i) needs an index not yet computed (still causal: only candles[0..i] exist at that point).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..backtest.engine import Signal
from ..features.indicators import adx, atr, bollinger, ema, rsi
from ..features.segments import GapPolicy, per_segment, segment_ids
from ..models import Candle
from ..sessions import XauCfd


class _FeatureStrategy:
    name = "base"
    hypothesis_id = ""

    def __init__(self, **params):
        self.p = dict(params)
        self._candles: Sequence[Candle] = ()
        self._n = 0
        self.f: dict[str, np.ndarray] = {}

    def params(self) -> dict:
        return dict(self.p)

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles
        self._n = 0
        self._compute()

    def _compute(self) -> None:
        c = list(self._candles)
        self._n = len(c)
        if not c:
            self.f = {}
            return
        ids = segment_ids(c, XauCfd(), GapPolicy.BREAK_ON_UNEXPECTED_GAP)
        h = np.array([x.high for x in c])
        l = np.array([x.low for x in c])
        cl = np.array([x.close for x in c])
        self.f = {"close": cl, "high": h, "low": l, "seg": ids}
        self.features(ids, h, l, cl)

    def features(self, ids, h, l, cl) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def decide(self, i: int) -> Signal | None:
        if i >= self._n:
            self._compute()  # live: the list grew since the last computation
        return self.rule(i)

    def rule(self, i: int) -> Signal | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _atr_ok(self, i: int) -> float | None:
        a = self.f["atr"][i]
        return float(a) if np.isfinite(a) and a > 0 else None

    def _signal(self, side: str, a: float) -> Signal:
        return Signal(side, round(self.p["stop_atr"] * a, 2), round(self.p["tp_atr"] * a, 2))


class Momentum(_FeatureStrategy):
    """H-XAU-001: trade in the direction of a fresh EMA(fast)/EMA(slow) cross when ADX confirms a trend."""

    name = "gold_momentum"
    hypothesis_id = "H-XAU-001"

    def features(self, ids, h, l, cl):
        p = self.p
        self.f["fast"] = per_segment(ema, ids, cl, n=p["fast"])
        self.f["slow"] = per_segment(ema, ids, cl, n=p["slow"])
        self.f["atr"] = per_segment(lambda hh, ll, cc: atr(hh, ll, cc, 14), ids, h, l, cl)
        self.f["adx"] = per_segment(lambda hh, ll, cc: adx(hh, ll, cc, 14)[0], ids, h, l, cl)

    def rule(self, i):
        if i < 1 or self.f["seg"][i] != self.f["seg"][i - 1]:
            return None
        f, a = self.f, self._atr_ok(i)
        if a is None or not np.isfinite(f["adx"][i]) or f["adx"][i] < self.p["adx_min"]:
            return None
        up_now, up_before = f["fast"][i] > f["slow"][i], f["fast"][i - 1] > f["slow"][i - 1]
        if not (np.isfinite(f["slow"][i - 1]) and up_now != up_before):
            return None  # only on a fresh cross
        return self._signal("buy" if up_now else "sell", a)


class TrendPullback(_FeatureStrategy):
    """H-XAU-002: in an established trend (close beyond a rising/falling EMA), enter when RSI recovers from a
    pullback back through its threshold."""

    name = "gold_pullback"
    hypothesis_id = "H-XAU-002"

    def features(self, ids, h, l, cl):
        p = self.p
        self.f["trend"] = per_segment(ema, ids, cl, n=p["trend_ema"])
        self.f["rsi"] = per_segment(rsi, ids, cl, n=14)
        self.f["atr"] = per_segment(lambda hh, ll, cc: atr(hh, ll, cc, 14), ids, h, l, cl)

    def rule(self, i):
        lb = self.p["slope_bars"]
        if i < lb or self.f["seg"][i] != self.f["seg"][i - lb]:
            return None
        f, a = self.f, self._atr_ok(i)
        t_now, t_then, r_now, r_prev = f["trend"][i], f["trend"][i - lb], f["rsi"][i], f["rsi"][i - 1]
        if a is None or not all(np.isfinite(x) for x in (t_now, t_then, r_now, r_prev)):
            return None
        lo, hi = self.p["rsi_pullback"], 100 - self.p["rsi_pullback"]
        if t_now > t_then and f["close"][i] > t_now and r_prev < lo <= r_now:
            return self._signal("buy", a)
        if t_now < t_then and f["close"][i] < t_now and r_prev > hi >= r_now:
            return self._signal("sell", a)
        return None


class RangeReversion(_FeatureStrategy):
    """H-XAU-003: when ADX says 'no trend', fade a close back inside the Bollinger band after a close outside it."""

    name = "gold_range"
    hypothesis_id = "H-XAU-003"

    def features(self, ids, h, l, cl):
        p = self.p
        self.f["bb_lo"] = per_segment(lambda c: bollinger(c, 20, p["bb_k"])[0], ids, cl)
        self.f["bb_hi"] = per_segment(lambda c: bollinger(c, 20, p["bb_k"])[2], ids, cl)
        self.f["atr"] = per_segment(lambda hh, ll, cc: atr(hh, ll, cc, 14), ids, h, l, cl)
        self.f["adx"] = per_segment(lambda hh, ll, cc: adx(hh, ll, cc, 14)[0], ids, h, l, cl)

    def rule(self, i):
        if i < 1 or self.f["seg"][i] != self.f["seg"][i - 1]:
            return None
        f, a = self.f, self._atr_ok(i)
        if a is None or not np.isfinite(f["adx"][i]) or f["adx"][i] >= self.p["adx_max"]:
            return None
        c_now, c_prev = f["close"][i], f["close"][i - 1]
        if np.isfinite(f["bb_lo"][i - 1]) and c_prev < f["bb_lo"][i - 1] and c_now > f["bb_lo"][i]:
            return self._signal("buy", a)
        if np.isfinite(f["bb_hi"][i - 1]) and c_prev > f["bb_hi"][i - 1] and c_now < f["bb_hi"][i]:
            return self._signal("sell", a)
        return None


class Breakout(_FeatureStrategy):
    """H-XAU-004: a close beyond the prior N-bar high/low while volatility is expanding (ATR above its own
    trailing median)."""

    name = "gold_breakout"
    hypothesis_id = "H-XAU-004"

    def features(self, ids, h, l, cl):
        n = self.p["lookback"]
        self.f["atr"] = per_segment(lambda hh, ll, cc: atr(hh, ll, cc, 14), ids, h, l, cl)
        prior_hi = np.full(len(cl), np.nan)
        prior_lo = np.full(len(cl), np.nan)
        atr_med = np.full(len(cl), np.nan)
        for i in range(len(cl)):
            s = i - n
            if s >= 0 and ids[s] == ids[i]:
                prior_hi[i] = h[s:i].max()  # bars BEFORE i only
                prior_lo[i] = l[s:i].min()
                window = self.f["atr"][s:i]
                window = window[np.isfinite(window)]
                if len(window):
                    atr_med[i] = np.median(window)
        self.f["prior_hi"], self.f["prior_lo"], self.f["atr_med"] = prior_hi, prior_lo, atr_med

    def rule(self, i):
        f, a = self.f, self._atr_ok(i)
        if a is None or not np.isfinite(f["prior_hi"][i]) or not np.isfinite(f["atr_med"][i]):
            return None
        if a < self.p["atr_expansion"] * f["atr_med"][i]:
            return None
        if f["close"][i] > f["prior_hi"][i]:
            return self._signal("buy", a)
        if f["close"][i] < f["prior_lo"][i]:
            return self._signal("sell", a)
        return None


GOLD_STRATEGIES = {cls.name: cls for cls in (Momentum, TrendPullback, RangeReversion, Breakout)}
