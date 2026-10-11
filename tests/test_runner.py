"""Experiment runner + ledger integration on the SYNTHETIC sample (software validation only)."""

from __future__ import annotations

import json

import pytest

from atlas.backtest.runner import ExperimentError, load_experiment, matched_random_percentile, run_experiment
from atlas.config import load_settings
from atlas.ingest import ingest
from atlas.providers import build_provider
from atlas.research.ledger import Ledger, LedgerError
from atlas.storage import Store

from .conftest import ROOT, FixedClock, utc_ms


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = Store(tmp_path_factory.mktemp("db") / "atlas.db")
    settings = load_settings(ROOT / "atlas.toml")
    gold = settings.instrument("XAUUSD")
    s.upsert_instruments([gold], 0)
    ingest(s, build_provider(settings.provider("sample_synthetic"), settings.root), gold, "M5",
           utc_ms(2026, 8, 2, 22), utc_ms(2026, 8, 28, 21), clock=FixedClock(utc_ms(2026, 10, 11)))
    yield s
    s.close()


def exp(name):
    return load_experiment(ROOT / f"experiments/{name}.toml", ROOT)


def test_run_is_recorded_and_reproducible(store):
    e = exp("baseline_random_entry_sample")
    id1, m1 = run_experiment(store, e, "train")
    id2, m2 = run_experiment(store, e, "train")
    led = Ledger(store)
    r1, r2 = led.get(id1), led.get(id2)
    assert r1["status"] == r2["status"] == "completed"
    assert r1["result_sha256"] == r2["result_sha256"] and r1["data_sha256"] == r2["data_sha256"]
    assert r1["config_sha256"] == r2["config_sha256"] and r1["data_label"] == "synthetic_sample"
    assert r1["cost_status"] == "provisional_dev" and "PROVISIONAL" in r1["spec_label"]
    assert len(led.trades(id1)) == m1["trades"] > 0
    cfg = json.loads(r1["config_json"])
    assert cfg["costs"]["status"] == "provisional_dev" and cfg["rules"].startswith("docs/13")
    sel = json.loads(r1["data_selection_json"])
    assert sel["period"] == "train" and sel["start_ms"] == utc_ms(2026, 8, 3)


def test_trades_stay_inside_the_period(store):
    e = exp("baseline_random_entry_sample")
    exp_id, _ = run_experiment(store, e, "validation")
    p = e.splits.get("validation")
    for t in Ledger(store).trades(exp_id):
        assert p.start_ms <= t["entry_time"] and t["exit_time"] <= p.end_ms


def test_stress_multiplier_changes_config_hash_and_costs(store):
    e = exp("baseline_session_long_sample")
    a, ma = run_experiment(store, e, "train")
    b, mb = run_experiment(store, e, "train", cost_multiplier=2.0)
    led = Ledger(store)
    assert led.get(a)["config_sha256"] != led.get(b)["config_sha256"]
    assert mb["net_pnl"] < ma["net_pnl"]


def test_test_period_guard(store):
    e = exp("baseline_no_trade_sample")
    with pytest.raises(ExperimentError, match="confirm"):
        run_experiment(store, e, "test")
    run_experiment(store, e, "test", confirm_test=True)
    with pytest.raises(LedgerError, match="already evaluated"):
        run_experiment(store, e, "test", confirm_test=True)


def test_failed_experiment_is_recorded(store, tmp_path):
    text = (ROOT / "experiments/baseline_no_trade_sample.toml").read_text().replace(
        'validation = ["2026-08-13", "2026-08-20"]', 'validation = ["2026-08-13", "2026-08-20"]').replace(
        'train = ["2026-08-03", "2026-08-12"]', 'train = ["2025-01-01", "2025-02-01"]')  # no data then
    (tmp_path / "x.toml").write_text(text)
    e = load_experiment(tmp_path / "x.toml", ROOT)
    with pytest.raises(ExperimentError, match="no stored candles"):
        run_experiment(store, e, "train")
    rec = Ledger(store).list(1)[0]
    assert rec["status"] == "failed" and "no stored candles" in rec["error"]


def test_r500_records_rejections_not_trades(store):
    exp_id, m = run_experiment(store, exp("r500_session_long_sample"), "train")
    assert m["trades"] == 0 and m["events"].get("risk_rejected", 0) > 0


def test_matched_random_percentile(store):
    out = matched_random_percentile(store, exp("baseline_random_entry_sample"), "train", seeds=20)
    assert 0.0 <= out["percentile"] <= 1.0 and out["seeds"] == 20
    assert (out["stop_usd"], out["tp_usd"]) == (3.0, 4.5)  # copied from the strategy, not supplied separately
    rec = Ledger(store).get(out["summary_experiment_id"])  # finding 6: the summary is ledgered
    assert rec["status"] == "completed" and rec["strategy"] == "matched_random"
    with pytest.raises(ExperimentError):
        matched_random_percentile(store, exp("baseline_random_entry_sample"), "test", seeds=2)


def test_invalid_experiment_configs(tmp_path):
    base = (ROOT / "experiments/baseline_no_trade_sample.toml").read_text()
    (tmp_path / "a.toml").write_text(base.replace('name = "no_trade"', 'name = "magic_profit"'))
    with pytest.raises(ExperimentError, match="unknown strategy"):
        load_experiment(tmp_path / "a.toml", ROOT)
    (tmp_path / "b.toml").write_text(base.replace('validation = ["2026-08-13"', 'validation = ["2026-08-12"'))
    from atlas.research.splits import SplitError
    with pytest.raises(SplitError):
        load_experiment(tmp_path / "b.toml", ROOT)


def test_finding_9_setup_failures_are_recorded(store, tmp_path):
    text = (ROOT / "experiments/baseline_random_entry_sample.toml").read_text().replace(
        "params = { seed = 1,", "params = { bogus = 1, seed = 1,")
    (tmp_path / "bad.toml").write_text(text)
    with pytest.raises(ExperimentError, match="setup failed"):
        run_experiment(store, load_experiment(tmp_path / "bad.toml", ROOT), "train")
    rec = Ledger(store).list(1)[0]
    assert rec["status"] == "failed" and rec["error"].startswith("setup:")
    with pytest.raises(ExperimentError, match="setup failed"):
        run_experiment(store, exp("baseline_no_trade_sample"), "train", cost_multiplier=0.5)
    assert Ledger(store).list(1)[0]["status"] == "failed"


def test_finding_12_data_labels_are_those_of_the_data_used(store):
    exp_id, m = run_experiment(store, exp("baseline_no_trade_sample"), "train")
    assert m["data_labels"] == ["synthetic_sample"]
