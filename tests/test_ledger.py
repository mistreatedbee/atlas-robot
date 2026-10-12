from __future__ import annotations

import json
import re
import sqlite3

import pytest

from atlas.research.ledger import Ledger, LedgerError, code_version, hash_candles, sha256_json
from atlas.research.splits import DAY_MS, Period, SplitError, Splits, required_embargo_ms
from atlas.storage import Store

from .conftest import make_candle, utc_ms


@pytest.fixture
def ledger():
    s = Store(":memory:")
    yield Ledger(s)
    s.close()


def register(ledger, period="validation", **kw):
    args = dict(name="t", strategy="no_trade", hypothesis_id="H-test", period=period, config={"a": 1}, data_selection={"tf": "M5"},
                cost_status="provisional_dev", spec_label="spec", now_ms=1, version="test")
    args.update(kw)
    return ledger.register(**args)


def test_register_complete_and_read_back(ledger):
    exp_id, cfg = register(ledger)
    assert ledger.get(exp_id)["status"] == "running"
    rsha = ledger.complete(exp_id, data_sha256="d" * 64, data_label="synthetic_sample", metrics={"net": -5.0},
                           trades=[{"pnl": -5.0}], events=[{"kind": "risk_rejected", "bar_time": 3, "detail": "x"}],
                           now_ms=2)
    rec = ledger.get(exp_id)
    assert rec["status"] == "completed" and rec["result_sha256"] == rsha and rec["config_sha256"] == cfg
    assert ledger.trades(exp_id) == [{"pnl": -5.0}]


def test_finished_experiments_are_immutable_and_undeletable(ledger):
    exp_id, _ = register(ledger)
    ledger.complete(exp_id, data_sha256="d", data_label="x", metrics={}, trades=[{"p": 1}], events=[], now_ms=2)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.conn.execute("UPDATE experiments SET metrics_json='{}' WHERE experiment_id=?", (exp_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("DELETE FROM experiments WHERE experiment_id=?", (exp_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("DELETE FROM experiment_trades")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("UPDATE experiment_trades SET trade_json='{}'")


def test_config_cannot_change_even_while_running(ledger):
    exp_id, _ = register(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.conn.execute("UPDATE experiments SET config_json='{}' WHERE experiment_id=?", (exp_id,))


def test_failed_experiments_are_kept(ledger):
    exp_id, _ = register(ledger)
    ledger.fail(exp_id, error="boom", now_ms=2)
    rec = ledger.get(exp_id)
    assert rec["status"] == "failed" and rec["error"] == "boom"
    with pytest.raises(LedgerError):
        ledger.complete(exp_id, data_sha256="d", data_label="x", metrics={}, trades=[], events=[], now_ms=3)


def test_repeat_test_period_use_is_refused_unless_overridden_and_recorded(ledger):
    first, _ = register(ledger, period="test")
    assert ledger.get(first)["test_access_override"] == 0
    with pytest.raises(LedgerError, match="already evaluated on the test period"):
        register(ledger, period="test")
    # changing parameters does not create a fresh test-set look for the same hypothesis
    with pytest.raises(LedgerError, match="already evaluated on the test period"):
        register(ledger, period="test", config={"a": 2})
    second, _ = register(ledger, period="test", allow_repeat_test=True)
    assert ledger.get(second)["test_access_override"] == 1 and ledger.test_runs("H-test") == 2
    register(ledger, period="validation")  # validation reruns are unrestricted
    register(ledger, period="test", hypothesis_id="H-other")  # a different hypothesis has its own first look


def test_hypothesis_id_required(ledger):
    with pytest.raises(LedgerError):
        register(ledger, hypothesis_id="")


def test_config_hash_is_canonical():
    assert sha256_json({"a": 1, "b": [1, 2]}) == sha256_json({"b": [1, 2], "a": 1})
    assert sha256_json({"a": 1}) != sha256_json({"a": 2})


def test_candle_hash_detects_any_change():
    a = [make_candle(utc_ms(2026, 7, 8, h)) for h in range(3)]
    b = list(a)
    b[1] = make_candle(utc_ms(2026, 7, 8, 1), close=101.01)
    assert hash_candles(a) == hash_candles(list(a)) and hash_candles(a) != hash_candles(b)


def test_code_version_reports_git_commit():
    v = code_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+\+([0-9a-f]{12}(\+dirty)?|unknown)", v)


def test_unknown_period_rejected(ledger):
    with pytest.raises(LedgerError):
        register(ledger, period="holdout")


# ---- splits ------------------------------------------------------------------------------------

def P(name, a, b):
    return Period(name, a, b)


def test_valid_chronological_splits():
    s = Splits(P("train", 0, 10 * DAY_MS), P("validation", 11 * DAY_MS, 20 * DAY_MS),
               P("test", 21 * DAY_MS, 30 * DAY_MS), embargo_ms=DAY_MS)
    assert s.get("test").start_ms == 21 * DAY_MS


@pytest.mark.parametrize("periods, embargo", [
    ((0, 10, 10, 20, 21, 30), 1),      # validation starts inside the embargo
    ((0, 10, 5, 20, 21, 30), 1),       # overlap
    ((0, 10, 21, 30, 11, 20), 1),      # out of order
    ((0, 10, 11, 20, 21, 30), 0.5),    # embargo too short
    ((10, 0, 11, 20, 21, 30), 1),      # empty period
])
def test_invalid_splits_rejected(periods, embargo):
    a, b, c, d, e, f = (x * DAY_MS for x in periods)
    with pytest.raises(SplitError):
        Splits(P("train", a, b), P("validation", c, d), P("test", e, f), embargo_ms=int(embargo * DAY_MS))


def test_embargo_must_cover_holding_period_and_lookback():
    m5 = 300_000
    need = required_embargo_ms(max_hold_bars=48, longest_lookback_bars=400, timeframe_ms=m5)  # 448 x 5 min > 1 day
    assert need == 448 * m5
    with pytest.raises(SplitError, match="shorter than required"):
        Splits(P("train", 0, 10 * DAY_MS), P("validation", 11 * DAY_MS, 20 * DAY_MS),
               P("test", 21 * DAY_MS, 30 * DAY_MS), embargo_ms=DAY_MS, min_embargo_ms=need)
    Splits(P("train", 0, 10 * DAY_MS), P("validation", 12 * DAY_MS, 20 * DAY_MS),
           P("test", 22 * DAY_MS, 30 * DAY_MS), embargo_ms=2 * DAY_MS, min_embargo_ms=need)


# ---- backtester review findings (ledger) -----------------------------------------------------------

def test_finding_4_no_rows_added_or_edited_after_completion(ledger):
    exp_id, _ = register(ledger)
    ledger.complete(exp_id, data_sha256="d", data_label="x", metrics={}, trades=[{"p": 1}],
                    events=[{"kind": "risk_rejected", "bar_time": 1}], now_ms=2)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("INSERT INTO experiment_trades VALUES (?, 99, '{}')", (exp_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("INSERT INTO experiment_events VALUES (?, 99, 1, 'filled', '')", (exp_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("UPDATE experiment_events SET kind='filled'")


def test_finding_4_running_rows_cannot_change_status_fields(ledger):
    exp_id, _ = register(ledger, period="test")
    for col, val in (("test_access_override", 0), ("cost_status", "'verified'"), ("spec_label", "'x'")):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger.conn.execute(f"UPDATE experiments SET {col}={val} WHERE experiment_id=?", (exp_id,))


def test_finding_4_result_hash_covers_events(ledger):
    a, _ = register(ledger, config={"x": 1})
    b, _ = register(ledger, config={"x": 2})
    ra = ledger.complete(a, data_sha256="d", data_label="x", metrics={}, trades=[], events=[{"kind": "k1"}], now_ms=2)
    rb = ledger.complete(b, data_sha256="d", data_label="x", metrics={}, trades=[], events=[{"kind": "k2"}], now_ms=2)
    assert ra != rb


SERIES = {"instrument": "XAUUSD", "provider": "p", "venue": "v", "timeframe": "M5"}


def sel(start, end):
    return {**SERIES, "start_ms": start, "end_ms": end}


def reserve(ledger, start, end, hyp="H-test"):
    """Register a train run that declares (and therefore reserves) a test window."""
    return register(ledger, period="train", hypothesis_id=hyp, data_selection=sel(0, 10), reserve_test_window=(start, end))


def test_finding_5_reserved_test_window_blocks_research_runs_on_it(ledger):
    reserve(ledger, 100, 200, hyp="H1")
    with pytest.raises(LedgerError, match="reserved"):
        register(ledger, period="validation", data_selection=sel(150, 250))   # test dates copied into validation
    with pytest.raises(LedgerError, match="reserved"):
        register(ledger, period="train", hypothesis_id="H-renamed", data_selection=sel(90, 110))
    register(ledger, period="validation", data_selection=sel(10, 100))         # adjacent, not overlapping: fine
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        ledger.conn.execute("DELETE FROM reserved_test_windows")


def test_recheck_A_reservation_covers_every_feed_and_timeframe(ledger):
    reserve(ledger, 100, 200)
    for other in ({"timeframe": "M1"}, {"provider": "vault_mt5", "venue": "vault_markets"}):
        with pytest.raises(LedgerError, match="reserved"):
            register(ledger, period="validation", data_selection={**sel(120, 130), **other})


def test_finding_5_cannot_reserve_already_researched_data_as_test(ledger):
    register(ledger, period="validation", data_selection=sel(0, 100))
    with pytest.raises(LedgerError, match="already used overlapping"):
        reserve(ledger, 50, 150)


def test_recheck_C_refused_reservation_leaves_nothing_behind(ledger):
    register(ledger, period="validation", data_selection=sel(0, 100))
    n_exp = ledger.conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    with pytest.raises(LedgerError):
        reserve(ledger, 50, 150)
    assert ledger.reserved_windows("XAUUSD") == []
    assert ledger.conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == n_exp


def test_finding_5_cost_stress_is_part_of_the_same_test_look(ledger):
    reserve(ledger, 100, 200)
    register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=1.0)
    register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=1.5)
    register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=2.0)
    with pytest.raises(LedgerError):  # exact repeat
        register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=2.0)
    with pytest.raises(LedgerError):  # a different configuration (or code version) = a second look
        register(ledger, period="test", data_selection=sel(100, 200), look_key="L2", cost_multiplier=1.0)
    other, _ = register(ledger, period="test", hypothesis_id="H-other", data_selection=sel(100, 200), look_key="L9")
    assert json.loads(ledger.get(other)["data_selection_json"])["test_window_prior_looks"] == 1  # H-test's one look


def test_recheck_A_only_standard_stress_multipliers_on_test(ledger):
    reserve(ledger, 100, 200)
    with pytest.raises(LedgerError, match="multiplier"):
        register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=1.0000001)


def test_recheck_A_prior_looks_ignore_own_look_and_disjoint_windows(ledger):
    reserve(ledger, 100, 200)
    reserve(ledger, 300, 400, hyp="H-far")
    register(ledger, period="test", hypothesis_id="H-far", data_selection=sel(300, 400), look_key="F")
    register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=1.0)
    stress, _ = register(ledger, period="test", data_selection=sel(100, 200), look_key="L1", cost_multiplier=1.5)
    assert json.loads(ledger.get(stress)["data_selection_json"])["test_window_prior_looks"] == 0


def test_recheck_legacy_test_runs_get_windows_backfilled():
    s = Store(":memory:")
    led = Ledger(s)
    register(led, period="test", data_selection=sel(100, 200), look_key="L")  # like a pre-v4 run: no reservation
    assert led.reserved_windows("XAUUSD") == []
    led2 = Ledger(s)  # re-opening backfills from existing test runs
    assert [(w["start_ms"], w["end_ms"]) for w in led2.reserved_windows("XAUUSD")] == [(100, 200)]
    with pytest.raises(LedgerError, match="reserved"):
        register(led2, period="validation", data_selection=sel(150, 160))


def test_finding_10_code_version_flags_untracked_files(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=tmp_path, check=True)
    assert not code_version(tmp_path).endswith("+dirty")
    (tmp_path / "new_strategy.py").write_text("x = 1")
    assert code_version(tmp_path).endswith("+dirty")


# ---- re-check 2 findings ---------------------------------------------------------------------------------

def test_recheck2_1_code_fingerprint_tracks_code_content_only(tmp_path):
    from atlas.research.ledger import code_fingerprint
    pkg = tmp_path / "atlas"
    (pkg / "backtest").mkdir(parents=True)
    (pkg / "backtest" / "engine.py").write_text("x = 1\n")
    base = code_fingerprint(pkg)
    (tmp_path / "notes_untracked.txt").write_text("hello")          # untracked note outside the package
    (tmp_path / "README.md").write_text("docs change")               # docs change
    assert code_fingerprint(pkg) == base
    (pkg / "backtest" / "costs.py").write_text("y = 2\n")             # an uncommitted code edit
    first_edit = code_fingerprint(pkg)
    assert first_edit != base
    (pkg / "backtest" / "costs.py").write_text("y = 3\n")             # a different edit -> different identity
    assert code_fingerprint(pkg) != first_edit


def test_recheck2_2_failure_records_are_not_test_looks_or_overrides(ledger):
    fid = ledger.record_failure(name="x", strategy="s", hypothesis_id="H-test", requested_period="test",
                                stage="refused", config={}, error="boom", now_ms=1, cost_multiplier=2.0)
    rec = ledger.get(fid)
    assert (rec["period"], rec["status"], rec["test_access_override"], rec["cost_multiplier"]) == ("full", "failed", 0, 2.0)
    assert json.loads(rec["data_selection_json"]) == {"requested_period": "test", "stage": "refused"}
    assert ledger.test_runs("H-test") == 0
    register(ledger, period="test")  # the hypothesis still has its single look available


def test_recheck2_3_register_takes_the_write_lock_before_checking(tmp_path):
    path = tmp_path / "shared.db"
    a, b = Store(path), Store(path)
    b.conn.execute("PRAGMA busy_timeout = 100")
    la, lb = Ledger(a), Ledger(b)
    a.conn.execute("BEGIN IMMEDIATE")  # process A is mid-transaction (e.g. reserving a window)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        register(lb, period="train", data_selection=sel(150, 160))  # B must wait, not read stale state
    a.conn.execute("ROLLBACK")
    register(lb, period="train", data_selection=sel(150, 160))
    a.close()
    b.close()
