"""Baselines and the R10 leakage test, on the SYNTHETIC XAUUSD sample (software validation only)."""

from __future__ import annotations

from collections import Counter

import pytest

from atlas.backtest.costs import CostModel
from atlas.backtest.engine import EngineConfig, Signal, run_backtest
from atlas.backtest.leakage import check_no_lookahead
from atlas.backtest.metrics import compute_metrics
from atlas.backtest.strategies import NoTrade, RandomEntry, SessionLong, matched_hourly_probabilities
from atlas.brokers.spec import load_spec
from atlas.models import DataLabel, PriceBasis, VolumeKind
from atlas.providers.csv_file import CsvFileProvider
from atlas.risk.gate import RiskLimits
from atlas.sessions import XauCfd, trading_day

from .conftest import ROOT, utc_ms

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
COSTS = CostModel(0.30, 0.80, 15, 0.10, 16.6, "provisional_dev", ("test-only values",))


@pytest.fixture(scope="module")
def sample():
    p = CsvFileProvider(name="sample_synthetic", venue="synthetic", path_template=ROOT / "data/sample/{symbol}_{timeframe}.csv",
                        data_label=DataLabel.SYNTHETIC_SAMPLE, price_basis=PriceBasis.BID, volume_kind=VolumeKind.SYNTHETIC)
    res = p.fetch("XAUUSD", "XAUUSD", "M5", utc_ms(2026, 8, 2), utc_ms(2026, 8, 29))
    # same exclusions the store applies: drop the documented invalid bar
    return [c for c in res.candles if c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close)]


def kwargs(equity=1_000_000.0, **cfg):
    return dict(spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                config=EngineConfig(initial_equity=equity, **cfg))


def test_no_trade_baseline_is_flat(sample):
    r = run_backtest(sample, NoTrade(), **kwargs())
    m = compute_metrics(r)
    assert m["trades"] == 0 and m["net_pnl"] == 0 and m["max_drawdown"] == 0


def test_random_entry_is_reproducible_and_respects_rules(sample):
    a = run_backtest(sample, RandomEntry(seed=1, p=0.02, stop_usd=3.0, tp_usd=4.5), **kwargs())
    b = run_backtest(sample, RandomEntry(seed=1, p=0.02, stop_usd=3.0, tp_usd=4.5), **kwargs())
    assert [t.record() for t in a.trades] == [t.record() for t in b.trades] and len(a.trades) > 20
    opens = {c.open_time: k for k, c in enumerate(sample)}
    closes = {c.close_time: k for k, c in enumerate(sample)}
    assert all(opens[t.entry_time] > closes[t.decision_time] for t in a.trades)  # fill bar after decision bar
    cal = XauCfd()
    for t in a.trades:
        assert t.entry_time >= t.decision_time  # decision at bar close; fill at a LATER bar's open
        assert cal.bar_in_session(t.entry_time, t.entry_time + 1)
        assert not cal.in_reopen_window(t.entry_time, COSTS.reopen_minutes)
        assert t.exit_reason in {"stop", "tp", "stop_gap", "tp_gap", "session_close", "end_of_data"}
    # flat before every scheduled closure: no trade spans a daily break or weekend
    assert all(trading_day(t.entry_time) == trading_day(t.exit_time - 1) for t in a.trades)
    c = run_backtest(sample, RandomEntry(seed=2, p=0.02, stop_usd=3.0, tp_usd=4.5), **kwargs())
    assert [t.entry_time for t in c.trades] != [t.entry_time for t in a.trades]


def test_session_long_once_per_day_after_reopen_window(sample):
    r = run_backtest(sample, SessionLong(stop_usd=25.0, reopen_minutes=15), **kwargs())
    days = Counter(t.trading_day for t in r.trades)
    assert r.trades and max(days.values()) == 1
    cal = XauCfd()
    assert all(not cal.in_reopen_window(t.entry_time, 15) and t.side == "buy" for t in r.trades)


def test_r500_account_cannot_trade_any_baseline(sample):
    r = run_backtest(sample, SessionLong(stop_usd=25.0, reopen_minutes=15), **kwargs(equity=500.0))
    assert r.trades == []
    rejections = [e for e in r.events if e.kind == "risk_rejected"]
    assert rejections and all("minimum_volume_within_risk_limit" in e.detail for e in rejections)


def test_matched_probabilities():
    class T:
        def __init__(self, h):
            self.decision_time = utc_ms(2026, 8, 4, h, 5)
    probs = matched_hourly_probabilities([T(10), T(10), T(14)], {10: 20, 14: 10, 3: 5})
    assert probs == {10: 0.1, 14: 0.1, 3: 0.0}


# ---- R10 leakage test --------------------------------------------------------------------------------

def _cuts(sample, n=20):
    idx = [round(k * (len(sample) - 2) / (n - 1)) for k in range(n)]
    # include cuts adjacent to closures: last bar before a gap and first bar after it
    for k in range(1, len(sample)):
        if sample[k].open_time != sample[k - 1].close_time:
            idx += [k - 1, k]
            break
    return sorted(set(idx))


@pytest.mark.parametrize("make", [
    lambda: RandomEntry(seed=7, p=0.03, stop_usd=2.0, tp_usd=3.0),
    lambda: SessionLong(stop_usd=25.0, reopen_minutes=15),
])
def test_baselines_pass_leakage_test(sample, make):
    data = sample[:2400]  # ~2 weeks of M5, includes weekends and daily breaks
    assert check_no_lookahead(make, data, _cuts(data), **kwargs()) == []


class PeeksAhead:
    """Deliberately leaky: buys when the NEXT bar closes higher."""

    name = "leaky"
    hypothesis_id = "TEST-leaky"

    def params(self):
        return {}

    def prepare(self, candles):
        self.c = candles

    def decide(self, i):
        if i + 1 < len(self.c) and self.c[i + 1].close > self.c[i].close + 1.0:
            return Signal("buy", 2.0, 3.0)
        return None


def test_leakage_test_detects_a_peeking_strategy(sample):
    data = sample[:1200]
    assert check_no_lookahead(PeeksAhead, data, _cuts(data), **kwargs())


class HoldsLong:
    """Signals every bar with a wide stop: at most cut points it is in a position, which the truncated run
    force-closes at end of data. Its raw decisions must still match (no false positive)."""

    name = "holds"
    hypothesis_id = "TEST-holds"

    def params(self):
        return {}

    def prepare(self, candles):
        pass

    def decide(self, i):
        return Signal("buy", 50.0, None)


def test_leakage_test_has_no_false_positive_for_open_positions_at_the_cut(sample):
    data = sample[:1200]
    assert check_no_lookahead(HoldsLong, data, _cuts(data), **kwargs()) == []
