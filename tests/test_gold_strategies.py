"""Gold strategy candidates: causality, live/incremental equivalence, pre-registration. SYNTHETIC data only:
nothing here is evidence about real gold; candidates are evaluated only on Vault data under pre-registration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.backtest.costs import CostModel
from atlas.backtest.engine import EngineConfig, run_backtest
from atlas.backtest.leakage import check_no_lookahead
from atlas.backtest.runner import ExperimentError, load_experiment, run_experiment
from atlas.brokers.spec import load_spec
from atlas.models import DataLabel, PriceBasis, VolumeKind
from atlas.providers.csv_file import CsvFileProvider
from atlas.research.hypotheses import HypothesisError, check_experiment, load_hypothesis, register
from atlas.risk.gate import RiskLimits
from atlas.sessions import XauCfd
from atlas.storage import Store
from atlas.strategies.gold import GOLD_STRATEGIES

from .conftest import ROOT, utc_ms

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
COSTS = CostModel(0.30, 0.80, 15, 0.10, 16.6, "provisional_dev", ("test-only values",))
HYP = {h.strategy: h for h in (load_hypothesis(p) for p in sorted((ROOT / "hypotheses").glob("*.toml")))}


def first_params(name):
    return {k: v[0] for k, v in HYP[name].parameters.items()}


@pytest.fixture(scope="module")
def sample():
    p = CsvFileProvider(name="sample_synthetic", venue="synthetic",
                        path_template=ROOT / "data/sample/{symbol}_{timeframe}.csv",
                        data_label=DataLabel.SYNTHETIC_SAMPLE, price_basis=PriceBasis.BID,
                        volume_kind=VolumeKind.SYNTHETIC)
    res = p.fetch("XAUUSD", "XAUUSD", "M5", utc_ms(2026, 8, 2), utc_ms(2026, 8, 29))
    return [c for c in res.candles if c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close)]


def kw():
    return dict(spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(), config=EngineConfig(initial_equity=1e6))


def test_every_registered_hypothesis_has_a_strategy_and_vice_versa():
    assert set(HYP) == set(GOLD_STRATEGIES)
    for name, cls in GOLD_STRATEGIES.items():
        assert cls.hypothesis_id == HYP[name].hypothesis_id


@pytest.mark.parametrize("name", sorted(GOLD_STRATEGIES))
def test_strategy_runs_and_signals(sample, name):
    r = run_backtest(sample, GOLD_STRATEGIES[name](**first_params(name)), **kw())
    assert any(e.kind == "decision" for e in r.events), "no signals at all on the synthetic sample"


@pytest.mark.parametrize("name", sorted(GOLD_STRATEGIES))
def test_strategy_has_no_look_ahead(sample, name):
    data = sample[:1500]
    cuts = sorted({round(k * (len(data) - 2) / 19) for k in range(20)})
    assert check_no_lookahead(lambda: GOLD_STRATEGIES[name](**first_params(name)), data, cuts, **kw()) == []


@pytest.mark.parametrize("name", sorted(GOLD_STRATEGIES))
def test_incremental_live_use_matches_batch(sample, name):
    data = sample[:900]
    batch = GOLD_STRATEGIES[name](**first_params(name))
    batch.prepare(data)
    live = GOLD_STRATEGIES[name](**first_params(name))
    grown: list = []
    live.prepare(grown)  # the paper session prepares once with a list that grows
    for i, c in enumerate(data):
        grown.append(c)
        assert live.decide(i) == batch.decide(i), f"bar {i}"


# ---- pre-registration ----------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "h.db")
    yield s
    s.close()


def test_register_is_permanent_and_content_locked(store, tmp_path):
    src = ROOT / "hypotheses/H-XAU-001_momentum.toml"
    h, new = register(store, src, now_ms=1)
    assert new and register(store, src, now_ms=2)[1] is False  # identical re-registration is a no-op
    edited = tmp_path / "edited.toml"
    edited.write_text(src.read_text(encoding="utf-8").replace("max_trials = 24", "max_trials = 200"), encoding="utf-8")
    with pytest.raises(HypothesisError, match="cannot change"):
        register(store, edited, now_ms=3)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE hypotheses SET spec_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM hypotheses")


def test_supersede_requires_existing(store, tmp_path):
    src = (ROOT / "hypotheses/H-XAU-001_momentum.toml").read_text(encoding="utf-8")
    v2 = tmp_path / "v2.toml"
    v2.write_text(src.replace('hypothesis_id = "H-XAU-001"', 'hypothesis_id = "H-XAU-001b"\nsupersedes = "H-XAU-001"'),
                  encoding="utf-8")
    with pytest.raises(HypothesisError, match="unknown"):
        register(store, v2, now_ms=1)
    register(store, ROOT / "hypotheses/H-XAU-001_momentum.toml", now_ms=1)
    assert register(store, v2, now_ms=2)[1]


def test_experiment_checks(store):
    register(store, ROOT / "hypotheses/H-XAU-003_range.toml", now_ms=1)
    ok = first_params("gold_range")
    check_experiment(store, "H-XAU-003", "gold_range", ok)
    check_experiment(store, "BASELINE-no_trade", "no_trade", {})  # baselines exempt
    with pytest.raises(HypothesisError, match="not registered"):
        check_experiment(store, "H-XAU-999", "gold_range", ok)
    with pytest.raises(HypothesisError, match="strategy"):
        check_experiment(store, "H-XAU-003", "gold_breakout", ok)
    with pytest.raises(HypothesisError, match="outside the registered grid"):
        check_experiment(store, "H-XAU-003", "gold_range", {**ok, "adx_max": 30})
    with pytest.raises(HypothesisError, match="differ"):
        check_experiment(store, "H-XAU-003", "gold_range", {k: v for k, v in ok.items() if k != "bb_k"})


def test_trial_budget_enforced(store):
    import json
    register(store, ROOT / "hypotheses/H-XAU-003_range.toml", now_ms=1)
    h = HYP["gold_range"]
    from itertools import product
    combos = [dict(zip(h.parameters, vals)) for vals in product(*h.parameters.values())]
    assert len(combos) == h.max_trials == 16
    for k, params in enumerate(combos[:16]):
        store.conn.execute(
            "INSERT INTO experiments (experiment_id, created_at_utc, name, strategy, hypothesis_id, period, status, "
            "config_json, config_sha256, data_selection_json, cost_status, spec_label, code_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"e{k}", k, "x", "gold_range", "H-XAU-003", "train", "running", json.dumps({"strategy_params": params}),
             "s", "{}", "c", "s", "v"))
    check_experiment(store, "H-XAU-003", "gold_range", combos[0])  # re-running a used set costs nothing
    # all 16 used: a hypothetical 17th distinct set would be refused (grid == budget here, so test via a fake grid)
    from atlas.research import hypotheses as hmod
    original = hmod.get
    hmod.get = lambda st, hid: hmod.Hypothesis("H-XAU-003", "gold_range", 16, {**h.parameters, "bb_k": [2.0, 2.5, 3.0]},
                                               {}, "x", None)
    try:
        with pytest.raises(HypothesisError, match="budget exhausted"):
            check_experiment(store, "H-XAU-003", "gold_range", {**combos[0], "bb_k": 3.0})
    finally:
        hmod.get = original


def test_vault_hypothesis_experiment_refused_until_registered_and_data_exists(store):
    exp = load_experiment(ROOT / "experiments/H-XAU-001_momentum_vault.toml", ROOT)
    with pytest.raises(ExperimentError, match="not registered"):
        run_experiment(store, exp, "train")
    register(store, ROOT / "hypotheses/H-XAU-001_momentum.toml", now_ms=1)
    with pytest.raises(ExperimentError):  # registered, but no Vault candles / FX stored: fails closed, recorded
        run_experiment(store, exp, "train")
