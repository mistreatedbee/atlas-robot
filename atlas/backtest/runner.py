"""Experiment runner: config file -> stored candles -> engine -> metrics -> append-only ledger."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..brokers.spec import SpecStatus, load_spec
from ..ingest import system_clock_ms
from ..models import timeframe_ms
from ..research.ledger import Ledger, hash_candles
from ..research.splits import DAY_MS, Period, Splits, required_embargo_ms
from ..risk.gate import RiskLimits
from ..sessions import XauCfd
from ..storage import Store
from .costs import CostModel
from .engine import EngineConfig, run_backtest
from .metrics import compute_metrics
from .strategies import NoTrade, RandomEntry, SessionLong, matched_hourly_probabilities

STRATEGIES = {"no_trade": NoTrade, "random_entry": RandomEntry, "session_long": SessionLong}


class ExperimentError(ValueError):
    pass


def _ms(day: str) -> int:
    dt = datetime.fromisoformat(day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class Experiment:
    path: Path
    raw: dict
    name: str
    hypothesis_id: str
    data: dict
    splits: Splits
    strategy_name: str
    strategy_params: dict
    engine: EngineConfig
    costs: CostModel
    spec_path: Path
    limits_path: Path
    warmup_bars: int


def load_experiment(path: str | Path, root: Path) -> Experiment:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    try:
        data = raw["data"]
        tf = timeframe_ms(data["timeframe"])
        eng = raw.get("engine", {})
        max_hold = eng.get("max_hold_bars") or None
        lookback = int(eng.get("longest_lookback_bars", 0))
        periods = raw["periods"]
        splits = Splits(
            Period("train", _ms(periods["train"][0]), _ms(periods["train"][1])),
            Period("validation", _ms(periods["validation"][0]), _ms(periods["validation"][1])),
            Period("test", _ms(periods["test"][0]), _ms(periods["test"][1])),
            embargo_ms=int(periods["embargo_days"] * DAY_MS),
            min_embargo_ms=required_embargo_ms(max_hold or 0, lookback, tf),
        )
        c = raw["costs"]
        costs = CostModel(c["base_spread_usd"], c["reopen_spread_usd"], int(c["reopen_minutes"]), c["slippage_usd"],
                          c["usd_to_account"], c["status"], tuple(c["sources"]))
        engine = EngineConfig(
            initial_equity=float(eng["initial_equity"]),
            latency_bars=int(eng.get("latency_bars", 0)),
            max_fill_wait_ms=int(eng.get("max_fill_wait_minutes", 5) * 60_000),
            pre_close_buffer_ms=int(eng.get("pre_close_buffer_minutes", 5) * 60_000),
            max_hold_bars=max_hold,
        )
        strat = raw["strategy"]
        if strat["name"] not in STRATEGIES:
            raise ExperimentError(f"unknown strategy {strat['name']!r}; known {sorted(STRATEGIES)}")
        return Experiment(path, raw, raw["name"], raw["hypothesis_id"], data, splits, strat["name"],
                          dict(strat.get("params", {})), engine, costs, root / raw["spec"],
                          root / raw.get("risk_limits", "config/risk.toml"), lookback)
    except KeyError as exc:
        raise ExperimentError(f"{path}: missing key {exc}") from None


def build_strategy(name: str, params: dict):
    return STRATEGIES[name](**params)


def _candles(store: Store, exp: Experiment, period: Period):
    d = exp.data
    tf = timeframe_ms(d["timeframe"])
    lookback_start = period.start_ms - max(exp.warmup_bars * tf * 3, DAY_MS)  # generous window, then trim
    candles = store.get_candles(d["instrument"], d["provider"], d["timeframe"], venue=d["venue"],
                                start_ms=lookback_start, end_ms=period.end_ms)
    before = [c for c in candles if c.open_time < period.start_ms][-exp.warmup_bars:] if exp.warmup_bars else []
    inside = [c for c in candles if c.open_time >= period.start_ms]
    if not inside:
        raise ExperimentError(f"no stored candles for {d} in period {period.name}")
    return before + inside


def run_experiment(store: Store, exp: Experiment, period_name: str, *, cost_multiplier: float = 1.0,
                   confirm_test: bool = False, allow_repeat_test: bool = False,
                   strategy_override=None, name_suffix: str = "") -> tuple[str, dict]:
    if period_name == "test" and not confirm_test:
        raise ExperimentError("test-period runs require explicit confirmation (--confirm-test); "
                              "each hypothesis gets one look at the test set")
    period = exp.splits.get(period_name)
    spec = load_spec(exp.spec_path)
    limits = RiskLimits.load(exp.limits_path)
    costs = exp.costs.scaled(cost_multiplier) if cost_multiplier != 1.0 else exp.costs
    cost_status = "verified" if (spec.status is SpecStatus.VERIFIED and costs.status == "verified") \
        else "provisional_dev"
    strategy = strategy_override or build_strategy(exp.strategy_name, exp.strategy_params)
    ledger = Ledger(store)
    config = {
        "strategy_params": strategy.params(), "engine": exp.engine.__dict__, "costs": costs.as_dict(),
        "risk_limits": limits.__dict__, "spec_sha256": spec.file_sha256, "cost_multiplier": cost_multiplier,
        "rules": "docs/13_simulation_rules.md v2", "warmup_bars": exp.warmup_bars,
    }
    selection = {**exp.data, "period": period_name, "start_ms": period.start_ms, "end_ms": period.end_ms}
    exp_id, _ = ledger.register(name=exp.name + name_suffix, strategy=strategy.name, hypothesis_id=exp.hypothesis_id,
                                period=period_name, config=config, data_selection=selection,
                                cost_status=cost_status, spec_label=spec.label, now_ms=system_clock_ms(),
                                allow_repeat_test=allow_repeat_test)
    try:
        candles = _candles(store, exp, period)
        result = run_backtest(candles, strategy, spec=spec, limits=limits, costs=costs, calendar=XauCfd(),
                              config=exp.engine, trade_start_ms=period.start_ms, trade_end_ms=period.end_ms)
        metrics = compute_metrics(result)
        labels = sorted(store.data_labels(exp.data["instrument"], exp.data["provider"], exp.data["timeframe"]))
        metrics["data_labels"] = labels
        metrics["cost_status"] = cost_status
        metrics["eligible_bars_by_hour"] = result.eligible_bars_by_hour
        ledger.complete(exp_id, data_sha256=hash_candles(candles), data_label=",".join(labels), metrics=metrics,
                        trades=[t.record() for t in result.trades],
                        events=[{"bar_time": e.time, "kind": e.kind, "detail": e.detail} for e in result.events
                                if e.kind != "decision"],
                        now_ms=system_clock_ms())
    except BaseException as exc:
        ledger.fail(exp_id, error=f"{type(exc).__name__}: {exc}", now_ms=system_clock_ms())
        raise
    metrics["_result"] = result
    return exp_id, metrics


def matched_random_percentile(store: Store, exp: Experiment, period_name: str, *, seeds: int,
                              stop_usd: float, tp_usd: float | None) -> dict:
    """R9.2: where does the strategy's net P&L rank among matched random-entry runs? Runs are NOT ledgered
    individually; the summary is ledgered as one experiment."""
    if period_name == "test":
        raise ExperimentError("matched random baselines are a research tool; run them on train/validation only")
    exp_id, m = run_experiment(store, exp, period_name)
    result = m.pop("_result")
    probs = matched_hourly_probabilities(result.trades, result.eligible_bars_by_hour)
    period = exp.splits.get(period_name)
    spec, limits = load_spec(exp.spec_path), RiskLimits.load(exp.limits_path)
    candles = _candles(store, exp, period)
    pnls = []
    for seed in range(seeds):
        r = run_backtest(candles, RandomEntry(seed=seed, p_by_hour=probs, stop_usd=stop_usd, tp_usd=tp_usd),
                         spec=spec, limits=limits, costs=exp.costs, calendar=XauCfd(), config=exp.engine,
                         trade_start_ms=period.start_ms, trade_end_ms=period.end_ms)
        pnls.append(r.equity_curve[-1][1] - r.initial_equity)
    below = sum(p < m["net_pnl"] for p in pnls)
    ties = sum(p == m["net_pnl"] for p in pnls)
    return {"strategy_experiment_id": exp_id, "strategy_net_pnl": m["net_pnl"], "seeds": seeds,
            "percentile": (below + 0.5 * ties) / seeds if seeds else math.nan,
            "random_net_pnl_median": sorted(pnls)[len(pnls) // 2] if pnls else None,
            "p_by_hour": probs}
