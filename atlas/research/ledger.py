"""Append-only experiment ledger.

Every backtest is registered BEFORE it runs (so crashes and failures are recorded too) and finalised once.
Finished records cannot be edited or deleted, and trades/events cannot be added after completion (SQLite triggers).
Failed and losing experiments stay forever.

Test-set discipline (docs/13 R7.3):
* Each experiment config declares its test window. The window is RESERVED per data series (instrument, provider,
  venue, timeframe). A window cannot be reserved if any earlier non-test experiment already used overlapping data,
  and once reserved, no train/validation run may overlap it - whatever its dates or hypothesis id say.
* A "look" at the test set is identified by the configuration WITHOUT the cost multiplier, so the R5.4 stress runs
  (x1.5, x2) belong to the same single look. A hypothesis gets one look; a different look, or an exact repeat,
  is refused unless explicitly overridden, and the override is recorded.
* Every test run records how many looks (by any hypothesis) the window has already had, for multiple-testing
  adjustment.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

from .. import __version__
from ..models import Candle
from ..storage import Store

PERIODS = ("train", "validation", "test", "full")


class LedgerError(RuntimeError):
    pass


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def hash_candles(candles: Sequence[Candle]) -> str:
    """Content hash of the exact candle rows a run used (order-sensitive)."""
    h = hashlib.sha256()
    for c in candles:
        h.update(canonical_json([c.instrument_id, c.provider, c.venue, c.timeframe, *c.values_key(), c.open_time])
                 .encode())
    return h.hexdigest()


def code_version(repo: Path | None = None) -> str:
    """atlas version + git commit, with '+dirty' if tracked changes OR new untracked (non-ignored) files exist."""
    repo = repo or Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        return f"{__version__}+{sha}{'+dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        return f"{__version__}+unknown"


SERIES_KEYS = ("instrument", "provider", "venue", "timeframe")


class Ledger:
    def __init__(self, store: Store):
        self.conn = store.conn

    # ---- test windows ------------------------------------------------------------------------------

    def reserve_test_window(self, series: dict, start_ms: int, end_ms: int, *, hypothesis_id: str, now_ms: int) -> None:
        key = tuple(series[k] for k in SERIES_KEYS)
        exists = self.conn.execute(
            "SELECT 1 FROM reserved_test_windows WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=? "
            "AND start_ms=? AND end_ms=?", (*key, start_ms, end_ms)).fetchone()
        if exists:
            return
        used = [r for r in self._experiments_on(key) if r["period"] != "test"
                and r["start_ms"] < end_ms and start_ms < r["end_ms"]]
        if used:
            raise LedgerError(
                f"cannot reserve test window: {len(used)} earlier non-test experiment(s) already used overlapping "
                f"data (e.g. {used[0]['experiment_id'][:8]}); a test set must be data nobody has looked at")
        with self.conn:
            self.conn.execute("INSERT INTO reserved_test_windows VALUES (?,?,?,?,?,?,?,?)",
                              (*key, start_ms, end_ms, hypothesis_id, now_ms))

    def _reserved(self, key: tuple) -> list:
        return self.conn.execute(
            "SELECT * FROM reserved_test_windows WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=?",
            key).fetchall()

    def _experiments_on(self, key: tuple) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT experiment_id, period, hypothesis_id, look_key, cost_multiplier, "
                                   "data_selection_json FROM experiments"):
            sel = json.loads(r["data_selection_json"])
            if tuple(sel.get(k) for k in SERIES_KEYS) == key and "start_ms" in sel:
                out.append({**dict(r), "start_ms": sel["start_ms"], "end_ms": sel["end_ms"]})
        return out

    def test_runs(self, hypothesis_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE hypothesis_id=? AND period='test'", (hypothesis_id,)
        ).fetchone()[0]

    # ---- experiments -------------------------------------------------------------------------------

    def register(self, *, name: str, strategy: str, hypothesis_id: str, period: str, config: dict, data_selection: dict,
                 cost_status: str, spec_label: str, now_ms: int, look_key: str | None = None,
                 cost_multiplier: float = 1.0, allow_repeat_test: bool = False,
                 version: str | None = None) -> tuple[str, str]:
        """Record an experiment before it runs. Returns (experiment_id, config_sha256)."""
        if period not in PERIODS:
            raise LedgerError(f"period must be one of {PERIODS}")
        if not hypothesis_id:
            raise LedgerError("hypothesis_id is required")
        config_sha = sha256_json({"config": config, "data_selection": data_selection, "strategy": strategy})
        look_key = look_key or config_sha
        override = 0
        series_known = all(k in data_selection for k in SERIES_KEYS) and "start_ms" in data_selection
        if series_known:
            key = tuple(data_selection[k] for k in SERIES_KEYS)
            start, end = data_selection["start_ms"], data_selection["end_ms"]
            if period != "test":
                for w in self._reserved(key):
                    if start < w["end_ms"] and w["start_ms"] < end:
                        raise LedgerError(
                            f"{period} period overlaps a reserved test window "
                            f"({w['start_ms']}..{w['end_ms']}, reserved by {w['reserved_by']}); "
                            "test data cannot be used for research")
        if period == "test":
            if series_known:
                prior = [r for r in self._experiments_on(tuple(data_selection[k] for k in SERIES_KEYS))
                         if r["period"] == "test"]
            else:  # no series information: fall back to all test runs (never to "no prior runs")
                prior = [dict(r) for r in self.conn.execute(
                    "SELECT experiment_id, period, hypothesis_id, look_key, cost_multiplier FROM experiments "
                    "WHERE period='test'")]
            mine = [r for r in prior if r["hypothesis_id"] == hypothesis_id]
            other_look = [r for r in mine if r["look_key"] != look_key]
            exact_repeat = [r for r in mine if r["look_key"] == look_key and r["cost_multiplier"] == cost_multiplier]
            if (other_look or exact_repeat) and not allow_repeat_test:
                raise LedgerError(
                    f"hypothesis {hypothesis_id!r} was already evaluated on the test period "
                    f"({len(mine)} run(s)); a different configuration or an exact repeat would be a second look at "
                    "the test set (override is recorded). Cost-stress runs of the same configuration are allowed.")
            override = int(bool(other_look or exact_repeat))
            data_selection = {**data_selection,
                              "test_window_prior_looks": len({(r["hypothesis_id"], r["look_key"]) for r in prior})}
        exp_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                """INSERT INTO experiments (experiment_id, created_at_utc, name, strategy, hypothesis_id, period, status,
                     config_json, config_sha256, data_selection_json, cost_status, spec_label, code_version,
                     test_access_override, look_key, cost_multiplier)
                   VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?)""",
                (exp_id, now_ms, name, strategy, hypothesis_id, period, canonical_json(config), config_sha,
                 canonical_json(data_selection), cost_status, spec_label, version or code_version(), override,
                 look_key, cost_multiplier),
            )
        return exp_id, config_sha

    def complete(self, exp_id: str, *, data_sha256: str, data_label: str, metrics: dict, trades: Iterable[dict],
                 events: Iterable[dict], now_ms: int) -> str:
        trades = list(trades)
        events = list(events)
        result_sha = sha256_json({"metrics": metrics, "trades": trades, "events": events, "data_sha256": data_sha256})
        with self.conn:
            self.conn.executemany("INSERT INTO experiment_trades VALUES (?,?,?)",
                                  [(exp_id, i, canonical_json(t)) for i, t in enumerate(trades)])
            self.conn.executemany("INSERT INTO experiment_events VALUES (?,?,?,?,?)",
                                  [(exp_id, i, e.get("bar_time"), e["kind"], e.get("detail")) for i, e in enumerate(events)])
            cur = self.conn.execute(
                """UPDATE experiments SET status='completed', finished_at_utc=?, data_sha256=?, data_label=?,
                     metrics_json=?, result_sha256=? WHERE experiment_id=? AND status='running'""",
                (now_ms, data_sha256, data_label, canonical_json(metrics), result_sha, exp_id))
            if cur.rowcount != 1:
                raise LedgerError(f"experiment {exp_id} is not running")
        return result_sha

    def fail(self, exp_id: str, *, error: str, now_ms: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE experiments SET status='failed', finished_at_utc=?, error=? "
                              "WHERE experiment_id=? AND status='running'", (now_ms, error[:2000], exp_id))

    def get(self, exp_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM experiments WHERE experiment_id=?", (exp_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown experiment {exp_id}")
        return dict(row)

    def trades(self, exp_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT trade_json FROM experiment_trades WHERE experiment_id=? ORDER BY seq", (exp_id,))]

    def list(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM experiments ORDER BY created_at_utc DESC, rowid DESC LIMIT ?", (limit,))]
