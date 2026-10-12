"""Paper session vs backtester on identical input (SYNTHETIC sample, PROVISIONAL costs; software validation)."""

from __future__ import annotations

import pytest

from atlas.backtest.costs import CostModel
from atlas.backtest.engine import EngineConfig, run_backtest
from atlas.backtest.strategies import RandomEntry
from atlas.broker.paper import PaperBroker
from atlas.broker.session import _with_crossings, run_paper_session
from atlas.brokers.spec import load_spec
from atlas.models import DataLabel, PriceBasis, VolumeKind
from atlas.providers.csv_file import CsvFileProvider
from atlas.risk.gate import KillSwitch, RiskLimits
from atlas.sessions import XauCfd
from atlas.storage import Store

from .conftest import ROOT, utc_ms

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
COSTS = CostModel(0.30, 0.80, 15, 0.10, 16.6, "provisional_dev", ("test-only values",))


@pytest.fixture(scope="module")
def sample():
    p = CsvFileProvider(name="sample_synthetic", venue="synthetic",
                        path_template=ROOT / "data/sample/{symbol}_{timeframe}.csv",
                        data_label=DataLabel.SYNTHETIC_SAMPLE, price_basis=PriceBasis.BID,
                        volume_kind=VolumeKind.SYNTHETIC)
    res = p.fetch("XAUUSD", "XAUUSD", "M5", utc_ms(2026, 8, 2), utc_ms(2026, 8, 29))
    return [c for c in res.candles if c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close)][:2400]


def make():
    return RandomEntry(seed=5, p=0.03, stop_usd=3.0, tp_usd=4.5)


def test_level_crossings_are_emitted_in_path_order():
    assert _with_crossings([10, 5, 20, 12], [7, 15]) == [10, 7, 5, 7, 15, 20, 15, 12]


def test_paper_session_matches_backtester_decisions(sample, tmp_path):
    bt = run_backtest(sample, make(), spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                      config=EngineConfig(initial_equity=1_000_000.0))
    store = Store(tmp_path / "paper.db")
    broker = PaperBroker(store, SPEC, kill_switch=KillSwitch(tmp_path / "KS"), slippage_usd=COSTS.slippage_usd)
    ps = run_paper_session(sample, make(), broker=broker, spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                           initial_equity=1_000_000.0, strategy_id="random_entry-5")
    assert len(bt.trades) > 20
    # identical entries: same decision bars, same fill bars, sides, lot sizes and entry prices
    bt_entries = [(t.decision_time, t.entry_time, t.side, round(t.lots, 8), round(t.entry_price, 6)) for t in bt.trades]
    ps_entries = [(x["decision_time"], x["time"], x["side"], round(x["lots"], 8), round(x["entry_price"], 6))
                  for x in ps.entries]
    assert ps_entries == bt_entries
    # exits: same reasons and same prices (gap fills are labelled stop/tp by the paper broker)
    assert len(ps.exits) == len(bt.trades)
    for t, x in zip(bt.trades, ps.exits):
        assert x["exit_reason"] == {"stop_gap": "stop", "tp_gap": "tp"}.get(t.exit_reason, t.exit_reason), (t, x)
        assert x["exit_price"] == pytest.approx(t.exit_price, abs=1e-6), (t, x)
    assert ps.final_equity == pytest.approx(bt.equity_curve[-1][1], abs=1e-4)
    # every paper order is journalled exactly once
    n = store.conn.execute("SELECT COUNT(*) FROM order_journal").fetchone()[0]
    assert n == len(ps.entries)
    store.close()


def test_paper_session_is_restart_safe(sample, tmp_path):
    """After a restart on the same database, every decision that was already handled is refused as a duplicate,
    and no client order id can ever be filled twice."""
    store = Store(tmp_path / "paper.db")
    ks = KillSwitch(tmp_path / "KS")
    kw = dict(spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(), initial_equity=1_000_000.0,
              strategy_id="random_entry-5")
    first = run_paper_session(sample, make(), broker=PaperBroker(store, SPEC, kill_switch=ks, slippage_usd=0.10), **kw)
    again = run_paper_session(sample, make(), broker=PaperBroker(store, SPEC, kill_switch=ks, slippage_usd=0.10), **kw)
    first_ids = {x["client_order_id"] for x in first.entries}
    assert first_ids and not first_ids & {x["client_order_id"] for x in again.entries}
    duplicates = [e for e in again.events if e[1] == "order_rejected" and e[2] == "duplicate"]
    assert len(duplicates) >= 1
    filled = store.conn.execute("SELECT client_order_id, COUNT(*) FROM order_journal WHERE status='filled' "
                                "GROUP BY client_order_id HAVING COUNT(*) > 1").fetchall()
    assert filled == []
    store.close()


def test_kill_switch_stops_new_paper_entries(sample, tmp_path):
    store = Store(tmp_path / "paper.db")
    ks = KillSwitch(tmp_path / "KS")
    ks.engage("test")
    ps = run_paper_session(sample, make(), broker=PaperBroker(store, SPEC, kill_switch=ks, slippage_usd=0.10),
                           spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(), initial_equity=1_000_000.0,
                           strategy_id="random_entry-5")
    assert ps.entries == [] and any("kill_switch" in e[2] for e in ps.events if e[1] == "risk_rejected")
    store.close()
