"""Experiment runner: config file -> stored candles -> engine -> metrics -> append-only ledger."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..brokers.spec import SpecStatus, load_spec
from ..ingest import system_clock_ms
from ..models import timeframe_ms
from ..research.ledger import Ledger, LedgerError, code_version, hash_candles, sha256_json
from ..research.splits import DAY_MS, Period, Splits, required_embargo_ms
from ..risk.gate import RiskLimits
from ..sessions import XauCfd
from ..storage import Store
from .costs import CostModel
from ..events import EventCalendar
from ..fx import FxSeries
from .engine import EngineConfig, NewsRules, run_backtest
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
    news: dict | None = None  # [news] section (None = no news handling)
    fx_source: str = "constant"  # "constant" (cost model rate) | "ecb_series"


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
        news = raw.get("news")
        if news is not None and not news.get("enabled", True):
            news = None
        if news is not None:
            news = {**news, "calendar": str(root / news["calendar"])}
        fx_source = raw.get("fx", {}).get("source", "constant")
        if fx_source not in ("constant", "ecb_series"):
            raise ExperimentError(f"[fx] source must be 'constant' or 'ecb_series', got {fx_source!r}")
        return Experiment(path, raw, raw["name"], raw["hypothesis_id"], data, splits, strat["name"],
                          dict(strat.get("params", {})), engine, costs, root / raw["spec"],
                          root / raw.get("risk_limits", "config/risk.toml"), lookback, news, fx_source)
    except KeyError as exc:
        raise ExperimentError(f"{path}: missing key {exc}") from None


def engine_extras(store: Store, exp: Experiment) -> tuple[dict, dict]:
    """Optional engine inputs (news calendar + rules, FX series) and their provenance for the ledger."""
    kwargs, provenance = {}, {"fx_source": exp.fx_source, "news": None}
    if exp.news is not None:
        n = exp.news
        path = Path(n["calendar"])
        kwargs["events_calendar"] = EventCalendar.load(path)
        kwargs["news"] = NewsRules(before_ms=int(n.get("before_minutes", 15) * 60_000),
                                   after_ms=int(n.get("after_minutes", 15) * 60_000),
                                   slippage_multiplier=float(n.get("slippage_multiplier", 3.0)),
                                   flat_before=bool(n.get("flat_before", True)), max_tier=int(n.get("max_tier", 1)))
        provenance["news"] = {**{k: v for k, v in n.items() if k != "calendar"}, "calendar_file": path.name,
                              "calendar_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if exp.fx_source == "ecb_series":
        kwargs["fx_series"] = FxSeries.load(store)
    return kwargs, provenance


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


def load_experiment_recorded(store: Store, path: str | Path, root: Path, period_name: str) -> Experiment:
    """load_experiment, but a config that cannot even be loaded is still recorded as a failed experiment."""
    try:
        return load_experiment(path, root)
    except Exception as exc:
        hypothesis = "unknown"
        try:
            with Path(path).open("rb") as fh:
                hypothesis = tomllib.load(fh).get("hypothesis_id") or "unknown"
        except Exception:
            pass
        ledger = Ledger(store)
        exp_id, _ = ledger.register(name=str(path), strategy="unknown", hypothesis_id=hypothesis,
                                    period=period_name if period_name in ("train", "validation", "test") else "full",
                                    config={"config_path": str(path), "stage": "load"},
                                    data_selection={"period": period_name}, cost_status="unknown",
                                    spec_label="unavailable", now_ms=system_clock_ms(), allow_repeat_test=True,
                                    cost_multiplier=1.0)
        ledger.fail(exp_id, error=f"load: {type(exc).__name__}: {exc}", now_ms=system_clock_ms())
        raise ExperimentError(f"experiment config could not be loaded (recorded in the ledger): {exc}") from exc


def _register_setup_failure(ledger: Ledger, exp: Experiment, period_name: str, exc: BaseException) -> str:
    """Finding 9: failures before a run starts (bad spec, limits, params, multiplier) are recorded too."""
    exp_id, _ = ledger.register(name=exp.name, strategy=exp.strategy_name, hypothesis_id=exp.hypothesis_id,
                                period=period_name, config={"raw_experiment": exp.raw, "stage": "setup"},
                                data_selection={"period": period_name}, cost_status="unknown",
                                spec_label="unavailable", now_ms=system_clock_ms(), allow_repeat_test=True)
    ledger.fail(exp_id, error=f"setup: {type(exc).__name__}: {exc}", now_ms=system_clock_ms())
    return exp_id


def run_experiment(store: Store, exp: Experiment, period_name: str, *, cost_multiplier: float = 1.0,
                   confirm_test: bool = False, allow_repeat_test: bool = False,
                   strategy_override=None, name_suffix: str = "") -> tuple[str, dict]:
    if period_name == "test" and not confirm_test:
        raise ExperimentError("test-period runs require explicit confirmation (--confirm-test); "
                              "each hypothesis gets one look at the test set")
    ledger = Ledger(store)
    series = {k: exp.data[k] for k in ("instrument", "provider", "venue", "timeframe")}
    test = exp.splits.get("test")
    period = exp.splits.get(period_name)
    try:
        spec = load_spec(exp.spec_path)
        limits = RiskLimits.load(exp.limits_path)
        base_costs = exp.costs
        base_costs.validate(spec.symbol.tick_size or 0.0)
        costs = base_costs.scaled(cost_multiplier) if cost_multiplier != 1.0 else base_costs
        strategy = strategy_override or build_strategy(exp.strategy_name, exp.strategy_params)
        extras, extras_provenance = engine_extras(store, exp)
    except Exception as exc:
        _register_setup_failure(ledger, exp, period_name, exc)
        raise ExperimentError(f"setup failed (recorded in the ledger): {type(exc).__name__}: {exc}") from exc
    cost_status = "verified" if (spec.status is SpecStatus.VERIFIED and costs.status == "verified") \
        else "provisional_dev"
    base_config = {
        "strategy_params": strategy.params(), "engine": exp.engine.__dict__, "base_costs": base_costs.as_dict(),
        "risk_limits": limits.__dict__, "spec_sha256": spec.file_sha256, "rules": "docs/13_simulation_rules.md v2.1",
        "warmup_bars": exp.warmup_bars, "code_version": code_version(),  # a code change = a new look
        **extras_provenance,
    }
    selection = {**series, "period": period_name, "start_ms": period.start_ms, "end_ms": period.end_ms}
    look_key = sha256_json({"config": base_config, "selection": selection, "strategy": strategy.name})
    config = {**base_config, "costs": costs.as_dict(), "cost_multiplier": cost_multiplier}
    try:
        # The config's test window is reserved atomically with this registration, before ANY run of it.
        exp_id, _ = ledger.register(name=exp.name + name_suffix, strategy=strategy.name,
                                    hypothesis_id=exp.hypothesis_id, period=period_name, config=config,
                                    data_selection=selection, cost_status=cost_status, spec_label=spec.label,
                                    now_ms=system_clock_ms(), look_key=look_key, cost_multiplier=cost_multiplier,
                                    allow_repeat_test=allow_repeat_test,
                                    reserve_test_window=(test.start_ms, test.end_ms))
    except LedgerError as exc:  # refusals (reservation conflict, test-set guard) are recorded too
        _register_setup_failure(ledger, exp, period_name, exc)
        raise
    try:
        candles = _candles(store, exp, period)
        result = run_backtest(candles, strategy, spec=spec, limits=limits, costs=costs, calendar=XauCfd(),
                              config=exp.engine, trade_start_ms=period.start_ms, trade_end_ms=period.end_ms, **extras)
        metrics = compute_metrics(result)
        labels = sorted(store.data_labels_in(series["instrument"], series["provider"], series["venue"],
                                             series["timeframe"], candles[0].open_time, candles[-1].close_time))
        metrics["data_labels"] = labels
        metrics["cost_status"] = cost_status
        metrics["decision_bars_by_hour"] = result.decision_bars_by_hour
        if period_name == "test":
            metrics["test_window_prior_looks"] = json.loads(ledger.get(exp_id)["data_selection_json"])[
                "test_window_prior_looks"]
        ledger.complete(exp_id, data_sha256=hash_candles(candles), data_label=",".join(labels), metrics=metrics,
                        trades=[t.record() for t in result.trades],
                        events=[{"bar_time": e.time, "kind": e.kind, "detail": e.detail} for e in result.events],
                        now_ms=system_clock_ms())
    except BaseException as exc:
        ledger.fail(exp_id, error=f"{type(exc).__name__}: {exc}", now_ms=system_clock_ms())
        raise
    metrics["_result"] = result
    return exp_id, metrics


def matched_random_percentile(store: Store, exp: Experiment, period_name: str, *, seeds: int) -> dict:
    """R9.2: rank the strategy's net P&L among random-entry runs matched on SIGNALS per UTC hour, with the
    strategy's own stop / take-profit, gate and window rules. The summary is recorded in the ledger."""
    if period_name == "test":
        raise ExperimentError("matched random baselines are a research tool; run them on train/validation only")
    strategy = build_strategy(exp.strategy_name, exp.strategy_params)
    stop_usd = getattr(strategy, "stop_usd", None)
    if not stop_usd:
        raise ExperimentError(f"strategy {exp.strategy_name} exposes no stop_usd; cannot build a matched baseline")
    tp_usd = getattr(strategy, "tp_usd", None)
    exp_id, m = run_experiment(store, exp, period_name)
    result = m.pop("_result")
    decisions = [e.time for e in result.events if e.kind == "decision"]
    probs = matched_hourly_probabilities(decisions, result.decision_bars_by_hour)
    period = exp.splits.get(period_name)
    spec, limits = load_spec(exp.spec_path), RiskLimits.load(exp.limits_path)
    candles = _candles(store, exp, period)
    extras, _ = engine_extras(store, exp)  # the random entries face the same news and FX rules
    pnls = []
    for seed in range(seeds):
        r = run_backtest(candles, RandomEntry(seed=seed, p_by_hour=probs, stop_usd=stop_usd, tp_usd=tp_usd),
                         spec=spec, limits=limits, costs=exp.costs, calendar=XauCfd(), config=exp.engine,
                         trade_start_ms=period.start_ms, trade_end_ms=period.end_ms, **extras)
        pnls.append(r.equity_curve[-1][1] - r.initial_equity)
    below = sum(p < m["net_pnl"] for p in pnls)
    ties = sum(p == m["net_pnl"] for p in pnls)
    summary = {"strategy_experiment_id": exp_id, "strategy_net_pnl": m["net_pnl"], "seeds": seeds,
               "percentile": (below + 0.5 * ties) / seeds if seeds else math.nan,
               "random_net_pnl_median": sorted(pnls)[len(pnls) // 2] if pnls else None,
               "random_net_pnls": pnls, "p_by_hour": {str(k): v for k, v in sorted(probs.items())},
               "stop_usd": stop_usd, "tp_usd": tp_usd}
    ledger = Ledger(store)
    series = {k: exp.data[k] for k in ("instrument", "provider", "venue", "timeframe")}
    sid, _ = ledger.register(name=f"Matched random baseline for {exp.name}", strategy="matched_random",
                             hypothesis_id=exp.hypothesis_id, period=period_name,
                             config={"strategy_experiment_id": exp_id, "seeds": seeds, "p_by_hour": summary["p_by_hour"],
                                     "stop_usd": stop_usd, "tp_usd": tp_usd},
                             data_selection={**series, "period": period_name, "start_ms": period.start_ms,
                                             "end_ms": period.end_ms},
                             cost_status=m["cost_status"], spec_label=spec.label, now_ms=system_clock_ms())
    ledger.complete(sid, data_sha256=hash_candles(candles), data_label=",".join(m["data_labels"]), metrics=summary,
                    trades=[], events=[], now_ms=system_clock_ms())
    summary["summary_experiment_id"] = sid
    return summary
