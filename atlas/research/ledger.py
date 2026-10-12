"""Append-only experiment ledger.

Every backtest is registered BEFORE it runs (so crashes and failures are recorded too) and finalised once.
Finished records cannot be edited or deleted, and trades/events cannot be added after completion (SQLite triggers).
Failed and losing experiments stay forever.

Test-set discipline (docs/13 R7.3-R7.6):
* Each experiment config declares its test window, RESERVED per INSTRUMENT (any provider, venue or timeframe) in the
  same transaction that registers the experiment. A window cannot be reserved on data an earlier non-test experiment
  used; once reserved, no train/validation run may overlap it, whatever its dates, feed or hypothesis id.
* A "look" = configuration (incl. code version) WITHOUT the cost multiplier. The stress runs x1.5 / x2 belong to the
  same look, once each; other multipliers are refused on the test set. A hypothesis gets one look; a different look
  or an exact repeat is refused unless overridden, and the override is recorded.
* Every test run records how many OTHER looks overlapping windows have had, for multiple-testing adjustment.
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


def code_fingerprint(package_dir: Path | None = None) -> str:
    """SHA-256 of the CONTENT of every atlas/**/*.py file as it is on disk now. Unlike the git commit, it changes
    exactly when the code that runs experiments changes: not for docs commits or untracked notes, but yes for
    uncommitted edits. Used to define a test-set "look"."""
    root = package_dir or Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(root)).encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


class _Immediate:
    """BEGIN IMMEDIATE transaction: takes the write lock BEFORE the checks, so concurrent processes cannot both
    pass a check and then write conflicting rows."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


SERIES_KEYS = ("instrument", "provider", "venue", "timeframe")
STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)  # the only cost multipliers allowed on the test set (docs/13 R5.4)
ANY = "*"


class Ledger:
    """Test windows are reserved per INSTRUMENT (across every provider, venue and timeframe): the same gold prices
    seen through another feed or bar size are still the test set."""

    def __init__(self, store: Store):
        self.conn = store.conn
        self._backfill_reservations()

    # ---- test windows ------------------------------------------------------------------------------

    def _test_experiments(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT experiment_id, period, hypothesis_id, look_key, cost_multiplier, status, "
                                   "created_at_utc, data_selection_json FROM experiments"):
            sel = json.loads(r["data_selection_json"])
            if "instrument" in sel and "start_ms" in sel:
                out.append({**dict(r), "instrument": sel["instrument"], "start_ms": sel["start_ms"],
                            "end_ms": sel["end_ms"]})
        return out

    def _backfill_reservations(self) -> None:
        """Test runs recorded before reservations existed (schema < 4) get their windows reserved retroactively."""
        try:
            rows = [r for r in self._test_experiments() if r["period"] == "test"]
        except Exception:  # tables not created yet in an unusual state: nothing to backfill
            return
        have = {(w["instrument_id"], w["start_ms"], w["end_ms"])
                for w in self.conn.execute("SELECT instrument_id, start_ms, end_ms FROM reserved_test_windows")}
        rows = [r for r in rows if (r["instrument"], r["start_ms"], r["end_ms"]) not in have]
        if not rows:
            return  # nothing to back-fill: opening the ledger does not write
        with _Immediate(self.conn):
            for r in rows:
                self.conn.execute("INSERT OR IGNORE INTO reserved_test_windows VALUES (?,?,?,?,?,?,?,?)",
                                  (r["instrument"], ANY, ANY, ANY, r["start_ms"], r["end_ms"], r["hypothesis_id"],
                                   r["created_at_utc"]))

    def reserved_windows(self, instrument: str) -> list:
        return self.conn.execute("SELECT * FROM reserved_test_windows WHERE instrument_id=?", (instrument,)).fetchall()

    def _reserve_in_txn(self, instrument: str, start_ms: int, end_ms: int, hypothesis_id: str, now_ms: int) -> None:
        """Must be called inside the transaction that registers the experiment declaring the window."""
        if any(w["start_ms"] == start_ms and w["end_ms"] == end_ms for w in self.reserved_windows(instrument)):
            return
        used = [r for r in self._test_experiments() if r["instrument"] == instrument and r["period"] != "test"
                and r["start_ms"] < end_ms and start_ms < r["end_ms"]]
        if used:
            raise LedgerError(
                f"cannot reserve test window: {len(used)} earlier non-test experiment(s) already used overlapping "
                f"{instrument} data (e.g. {used[0]['experiment_id'][:8]}); a test set must be data nobody has looked at")
        self.conn.execute("INSERT INTO reserved_test_windows VALUES (?,?,?,?,?,?,?,?)",
                          (instrument, ANY, ANY, ANY, start_ms, end_ms, hypothesis_id, now_ms))

    def test_runs(self, hypothesis_id: str) -> int:
        """Registered test-period runs of a hypothesis (failure records are stored as 'full' and never count)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE hypothesis_id=? AND period='test'", (hypothesis_id,)
        ).fetchone()[0]

    # ---- experiments -------------------------------------------------------------------------------

    def register(self, *, name: str, strategy: str, hypothesis_id: str, period: str, config: dict, data_selection: dict,
                 cost_status: str, spec_label: str, now_ms: int, look_key: str | None = None,
                 cost_multiplier: float = 1.0, allow_repeat_test: bool = False,
                 reserve_test_window: tuple[int, int] | None = None,
                 version: str | None = None) -> tuple[str, str]:
        """Record an experiment before it runs (atomically with any test-window reservation it declares).
        Returns (experiment_id, config_sha256)."""
        if period not in PERIODS:
            raise LedgerError(f"period must be one of {PERIODS}")
        if not hypothesis_id:
            raise LedgerError("hypothesis_id is required")
        config_sha = sha256_json({"config": config, "data_selection": data_selection, "strategy": strategy})
        look_key = look_key or config_sha
        instrument = data_selection.get("instrument")
        has_range = instrument is not None and "start_ms" in data_selection
        exp_id = str(uuid.uuid4())
        with _Immediate(self.conn):  # one locked transaction: reservation + checks + insert
            if reserve_test_window is not None:
                if instrument is None:
                    raise LedgerError("a test window can only be reserved for a known instrument")
                self._reserve_in_txn(instrument, *reserve_test_window, hypothesis_id, now_ms)
            override = 0
            if has_range and period != "test":
                start, end = data_selection["start_ms"], data_selection["end_ms"]
                for w in self.reserved_windows(instrument):
                    if start < w["end_ms"] and w["start_ms"] < end:
                        raise LedgerError(
                            f"{period} period overlaps a reserved {instrument} test window "
                            f"({w['start_ms']}..{w['end_ms']}, reserved by {w['reserved_by']}); "
                            "test data cannot be used for research, from any provider or timeframe")
            if period == "test":
                if round(cost_multiplier, 9) not in STRESS_MULTIPLIERS:
                    raise LedgerError(f"test-period cost multiplier must be one of {STRESS_MULTIPLIERS}")
                if has_range:
                    s0, e0 = data_selection["start_ms"], data_selection["end_ms"]
                    prior = [r for r in self._test_experiments() if r["period"] == "test"
                             and r["instrument"] == instrument and r["start_ms"] < e0 and s0 < r["end_ms"]]
                else:  # no range information: fall back to all test runs (never to "no prior runs")
                    prior = [dict(r) for r in self.conn.execute(
                        "SELECT experiment_id, period, hypothesis_id, look_key, cost_multiplier FROM experiments "
                        "WHERE period='test'")]
                mine = [r for r in prior if r["hypothesis_id"] == hypothesis_id]
                other_look = [r for r in mine if r["look_key"] != look_key]
                exact_repeat = [r for r in mine if r["look_key"] == look_key
                                and round(r["cost_multiplier"] or 1.0, 9) == round(cost_multiplier, 9)]
                if (other_look or exact_repeat) and not allow_repeat_test:
                    raise LedgerError(
                        f"hypothesis {hypothesis_id!r} was already evaluated on the test period "
                        f"({len(mine)} run(s)); a different configuration or code version, or an exact repeat, would "
                        "be a second look at the test set (override is recorded). Stress runs x1.5 / x2 of the same "
                        "configuration are allowed once each.")
                override = int(bool(other_look or exact_repeat))
                looks = {(r["hypothesis_id"], r["look_key"]) for r in prior} - {(hypothesis_id, look_key)}
                data_selection = {**data_selection, "test_window_prior_looks": len(looks)}
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

    def record_failure(self, *, name: str, strategy: str, hypothesis_id: str, requested_period: str, stage: str,
                       config: dict, error: str, now_ms: int, cost_multiplier: float = 1.0) -> str:
        """A run that never reached the data (load/setup failure, or a refusal by the guards). Stored as a failed
        experiment with period 'full', no data range and no override flag, so it can never count as a test-set
        look, consume a reservation or be mistaken for an override."""
        exp_id = str(uuid.uuid4())
        selection = {"requested_period": requested_period, "stage": stage}
        with _Immediate(self.conn):
            self.conn.execute(
                """INSERT INTO experiments (experiment_id, created_at_utc, finished_at_utc, name, strategy,
                     hypothesis_id, period, status, config_json, config_sha256, data_selection_json, cost_status,
                     spec_label, code_version, test_access_override, look_key, cost_multiplier, error)
                   VALUES (?,?,?,?,?,?,'full','failed',?,?,?,'unknown','unavailable',?,0,NULL,?,?)""",
                (exp_id, now_ms, now_ms, name, strategy, hypothesis_id or "unknown", canonical_json(config),
                 sha256_json(config), canonical_json(selection), code_version(), cost_multiplier,
                 f"{stage}: {error}"[:2000]))
        return exp_id

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
