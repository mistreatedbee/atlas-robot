"""Append-only experiment ledger.

Every backtest is registered BEFORE it runs (so crashes and failures are recorded too) and finalised once.
Finished records cannot be edited or deleted (SQLite triggers). Failed and losing experiments stay forever.

Test-period discipline: once a HYPOTHESIS has been evaluated on the test period, any further test run of that
hypothesis (with any parameters) is refused unless explicitly overridden; the override is recorded.
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
    """atlas version + git commit, with '+dirty' if uncommitted changes exist. Never raises."""
    repo = repo or Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                               capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        return f"{__version__}+{sha}{'+dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        return f"{__version__}+unknown"


class Ledger:
    def __init__(self, store: Store):
        self.conn = store.conn

    def test_runs(self, hypothesis_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE hypothesis_id=? AND period='test'", (hypothesis_id,)
        ).fetchone()[0]

    def register(self, *, name: str, strategy: str, hypothesis_id: str, period: str, config: dict, data_selection: dict,
                 cost_status: str, spec_label: str, now_ms: int, allow_repeat_test: bool = False,
                 version: str | None = None) -> tuple[str, str]:
        """Record an experiment before it runs. Returns (experiment_id, config_sha256)."""
        if period not in PERIODS:
            raise LedgerError(f"period must be one of {PERIODS}")
        if not hypothesis_id:
            raise LedgerError("hypothesis_id is required")
        config_sha = sha256_json({"config": config, "data_selection": data_selection, "strategy": strategy})
        override = 0
        if period == "test":
            previous = self.test_runs(hypothesis_id)
            if previous and not allow_repeat_test:
                raise LedgerError(
                    f"hypothesis {hypothesis_id!r} was already evaluated on the test period {previous} time(s); "
                    "repeated test-set use invalidates it as out-of-sample evidence (override is recorded)")
            override = int(bool(previous))
        exp_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                """INSERT INTO experiments (experiment_id, created_at_utc, name, strategy, hypothesis_id, period, status,
                     config_json, config_sha256, data_selection_json, cost_status, spec_label, code_version,
                     test_access_override)
                   VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,?)""",
                (exp_id, now_ms, name, strategy, hypothesis_id, period, canonical_json(config), config_sha,
                 canonical_json(data_selection), cost_status, spec_label, version or code_version(), override),
            )
        return exp_id, config_sha

    def complete(self, exp_id: str, *, data_sha256: str, data_label: str, metrics: dict, trades: Iterable[dict],
                 events: Iterable[dict], now_ms: int) -> str:
        trades = list(trades)
        events = list(events)
        result_sha = sha256_json({"metrics": metrics, "trades": trades, "data_sha256": data_sha256})
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
