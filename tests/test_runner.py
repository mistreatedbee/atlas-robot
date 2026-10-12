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
    # TEST-ONLY USD/ZAR rates (not ECB data) covering the sample, so no network is needed
    from datetime import date, timedelta
    from atlas.fx import store_rates
    days = [(date(2026, 7, 27) + timedelta(days=k)).isoformat() for k in range(40)]
    store_rates(s, "USDZAR", [(d, 16.5) for d in days], now_ms=0)
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
    text = (ROOT / "experiments/baseline_no_trade_sample.toml").read_text(encoding="utf-8").replace(
        'validation = ["2026-08-13", "2026-08-20"]', 'validation = ["2026-08-13", "2026-08-20"]').replace(
        'train = ["2026-08-03", "2026-08-12"]', 'train = ["2025-01-01", "2025-02-01"]')  # no data then
    (tmp_path / "x.toml").write_text(text, encoding="utf-8")
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
    base = (ROOT / "experiments/baseline_no_trade_sample.toml").read_text(encoding="utf-8")
    (tmp_path / "a.toml").write_text(base.replace('name = "no_trade"', 'name = "magic_profit"'), encoding="utf-8")
    with pytest.raises(ExperimentError, match="unknown strategy"):
        load_experiment(tmp_path / "a.toml", ROOT)
    (tmp_path / "b.toml").write_text(base.replace('validation = ["2026-08-13"', 'validation = ["2026-08-12"'), encoding="utf-8")
    from atlas.research.splits import SplitError
    with pytest.raises(SplitError):
        load_experiment(tmp_path / "b.toml", ROOT)


def test_finding_9_setup_failures_are_recorded(store, tmp_path):
    text = (ROOT / "experiments/baseline_random_entry_sample.toml").read_text(encoding="utf-8").replace(
        "params = { seed = 1,", "params = { bogus = 1, seed = 1,")
    (tmp_path / "bad.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ExperimentError, match="setup failed"):
        run_experiment(store, load_experiment(tmp_path / "bad.toml", ROOT), "train")
    rec = Ledger(store).list(1)[0]
    assert rec["status"] == "failed" and rec["error"].startswith("setup:")
    with pytest.raises(ExperimentError, match="setup failed"):
        run_experiment(store, exp("baseline_no_trade_sample"), "train", cost_multiplier=0.5)
    assert Ledger(store).list(1)[0]["status"] == "failed"


def test_finding_12_data_labels_are_those_of_the_data_used(store):
    # a row with ANOTHER label exists for the same instrument/provider/timeframe but outside the train data
    from atlas.models import DataLabel, ValidationStatus
    from .conftest import make_candle
    from atlas.models import PriceBasis
    run_id = store.start_run(instrument_id="XAUUSD", provider="sample_synthetic", venue="other_venue", timeframe="M5",
                             source_symbol="XAUUSD", data_label=DataLabel.USER_FILE, start_ms=0, end_ms=1, now_ms=0)
    c = make_candle(utc_ms(2026, 8, 4, 10), tf="M5", instrument_id="XAUUSD", provider="sample_synthetic",
                    venue="other_venue", price_basis=PriceBasis.BID)
    store.commit_chunk(run_id=run_id, data_label=DataLabel.USER_FILE, candles=[(c, ValidationStatus.VALID, [])],
                       rejects=[], checkpoint_ms=1, now_ms=0, extra_counts={})
    assert store.data_labels("XAUUSD", "sample_synthetic", "M5") == {"synthetic_sample", "user_file"}  # old query
    exp_id, m = run_experiment(store, exp("baseline_no_trade_sample"), "train")
    assert m["data_labels"] == ["synthetic_sample"]


def test_recheck_unloadable_config_is_recorded(store, tmp_path):
    from atlas.backtest.runner import load_experiment_recorded
    bad = tmp_path / "broken.toml"
    bad.write_text('hypothesis_id = "H-broken"\nname = "x"\n', encoding="utf-8")  # missing everything else
    with pytest.raises(ExperimentError, match="could not be loaded"):
        load_experiment_recorded(store, bad, ROOT, "validation")
    rec = Ledger(store).list(1)[0]
    assert rec["status"] == "failed" and rec["hypothesis_id"] == "H-broken" and rec["error"].startswith("load:")


def test_news_and_fx_are_used_and_recorded(store):
    exp_id, m = run_experiment(store, exp("baseline_session_long_sample"), "train")
    cfg = json.loads(Ledger(store).get(exp_id)["config_json"])
    assert cfg["fx_source"] == "ecb_series" and m["fx_source"].startswith("ECB")
    assert cfg["news"]["calendar_file"] == "us_tier1.csv" and len(cfg["news"]["calendar_sha256"]) == 64
    # session-long holds all day, so the NFP release of Fri 7 Aug 2026 (12:30 UTC) forces a pre-news exit
    news_exits = [t for t in Ledger(store).trades(exp_id) if t["exit_reason"] == "news_exit"]
    assert [t["exit_time"] for t in news_exits] == [utc_ms(2026, 8, 7, 12, 15)]


def test_missing_fx_rates_fail_closed_and_are_recorded(tmp_path):
    from atlas.config import load_settings
    s = Store(tmp_path / "empty.db")
    with pytest.raises(ExperimentError, match="FxError"):
        run_experiment(s, exp("baseline_no_trade_sample"), "train")
    assert Ledger(s).list(1)[0]["status"] == "failed"
    s.close()
