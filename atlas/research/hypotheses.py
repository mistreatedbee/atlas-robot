"""Pre-registration of strategy hypotheses (docs/07 "Before testing").

A hypothesis file (hypotheses/*.toml) states the rules, the allowed parameter grid, a trial budget and the success
criteria BEFORE any test. Registering stores it permanently (immutable rows); a changed idea is a NEW hypothesis
that `supersedes` the old one. The experiment runner refuses any non-baseline experiment whose hypothesis is not
registered, whose strategy or parameters fall outside the registered grid, or that would exceed the trial budget.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..storage import Store

REQUIRED = ("hypothesis_id", "family", "strategy", "statement", "execution_tf", "entry_rules", "exit_rules",
            "stop", "invalidation", "no_trade_conditions", "baseline", "cost_model", "max_trials",
            "success_criteria", "parameters")
FAMILIES = {"momentum", "pullback", "range", "breakout"}
BASELINE_PREFIX = "BASELINE-"


class HypothesisError(ValueError):
    pass


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    strategy: str
    max_trials: int
    parameters: dict[str, list]
    spec: dict
    sha256: str
    supersedes: str | None


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def load_hypothesis(path: str | Path) -> Hypothesis:
    raw_bytes = Path(path).read_bytes()
    spec = tomllib.loads(raw_bytes.decode("utf-8"))
    missing = [k for k in REQUIRED if k not in spec]
    if missing:
        raise HypothesisError(f"{path}: missing {missing}")
    if spec["family"] not in FAMILIES:
        raise HypothesisError(f"{path}: family must be one of {sorted(FAMILIES)}")
    if spec["hypothesis_id"].startswith(BASELINE_PREFIX):
        raise HypothesisError("baselines are not hypotheses")
    grid = spec["parameters"]
    if not grid or not all(isinstance(v, list) and v for v in grid.values()):
        raise HypothesisError(f"{path}: every parameter needs a non-empty list of allowed values")
    if not (isinstance(spec["max_trials"], int) and 1 <= spec["max_trials"] <= 200):
        raise HypothesisError(f"{path}: max_trials must be an integer in [1, 200]")
    for k in ("min_oos_trades", "beats_random_percentile"):
        if k not in spec["success_criteria"]:
            raise HypothesisError(f"{path}: success_criteria must include {k}")
    sha = hashlib.sha256(_canonical(spec).encode()).hexdigest()
    return Hypothesis(spec["hypothesis_id"], spec["strategy"], spec["max_trials"], grid, spec, sha,
                      spec.get("supersedes"))


def register(store: Store, path: str | Path, *, now_ms: int) -> tuple[Hypothesis, bool]:
    """Register a hypothesis file. Returns (hypothesis, newly_registered). Re-registering identical content is a
    no-op; different content under an existing id is refused (it must be a new, superseding hypothesis)."""
    h = load_hypothesis(path)
    row = store.conn.execute("SELECT spec_sha256 FROM hypotheses WHERE hypothesis_id=?", (h.hypothesis_id,)).fetchone()
    if row is not None:
        if row[0] != h.sha256:
            raise HypothesisError(f"{h.hypothesis_id} is already registered with different content; registered "
                                  "hypotheses cannot change - create a new id with `supersedes`")
        return h, False
    if h.supersedes and get(store, h.supersedes) is None:
        raise HypothesisError(f"supersedes unknown hypothesis {h.supersedes}")
    with store.conn:
        store.conn.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?)",
                           (h.hypothesis_id, now_ms, h.strategy, Path(path).name, _canonical(h.spec), h.sha256,
                            h.supersedes))
    return h, True


def get(store: Store, hypothesis_id: str) -> Hypothesis | None:
    row = store.conn.execute("SELECT spec_json, spec_sha256 FROM hypotheses WHERE hypothesis_id=?",
                             (hypothesis_id,)).fetchone()
    if row is None:
        return None
    spec = json.loads(row[0])
    return Hypothesis(spec["hypothesis_id"], spec["strategy"], spec["max_trials"], spec["parameters"], spec, row[1],
                      spec.get("supersedes"))


def trials_used(store: Store, hypothesis_id: str) -> set[str]:
    """Distinct parameter sets already tried (any non-failure-record experiment of the hypothesis)."""
    used = set()
    for (cfg,) in store.conn.execute("SELECT config_json FROM experiments WHERE hypothesis_id=? AND period!='full'",
                                     (hypothesis_id,)):
        params = json.loads(cfg).get("strategy_params")
        if params is not None:
            used.add(_canonical(params))
    return used


def check_experiment(store: Store, hypothesis_id: str, strategy: str, params: dict) -> None:
    """Refuse experiments outside the pre-registration. Baselines are exempt."""
    if hypothesis_id.startswith(BASELINE_PREFIX):
        return
    h = get(store, hypothesis_id)
    if h is None:
        raise HypothesisError(f"hypothesis {hypothesis_id} is not registered; run `atlas hypothesis register` first")
    if strategy != h.strategy:
        raise HypothesisError(f"{hypothesis_id} registers strategy {h.strategy!r}, experiment uses {strategy!r}")
    if set(params) != set(h.parameters):
        raise HypothesisError(f"parameters {sorted(params)} differ from the registered grid {sorted(h.parameters)}")
    outside = {k: v for k, v in params.items() if v not in h.parameters[k]}
    if outside:
        raise HypothesisError(f"parameter values outside the registered grid: {outside}")
    used = trials_used(store, hypothesis_id)
    if _canonical(params) not in used and len(used) >= h.max_trials:
        raise HypothesisError(f"{hypothesis_id} trial budget exhausted ({len(used)}/{h.max_trials} parameter sets)")
