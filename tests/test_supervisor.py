from __future__ import annotations

import json

import pytest

from atlas.monitoring import HealthReport, check_heartbeat
from atlas.supervisor import Supervisor


class Clock:
    def __init__(self):
        self.t = 1_800_000_000_000

    def __call__(self):
        return self.t


def make(tmp_path, job, **kw):
    clock, sleeps = Clock(), []

    def sleep(s):
        sleeps.append(s)
        clock.t += int(s * 1000)

    sup = Supervisor(job, heartbeat=tmp_path / "logs" / "heartbeat.json", alerts=tmp_path / "logs" / "alerts.jsonl",
                     stop_file=tmp_path / "STOP", clock=clock, sleep=sleep, **kw)
    return sup, clock, sleeps


def test_restarts_after_crashes_with_backoff_then_stops_on_stop_file(tmp_path):
    calls = []

    def job():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("terminal disconnected")
        (tmp_path / "STOP").write_text("stop", encoding="utf-8")  # a clean run, then the owner asks to stop
        return 0

    sup, clock, sleeps = make(tmp_path, job, base_backoff_s=5, max_backoff_s=300)
    res = sup.run()
    assert (res.runs, res.crashes, res.stopped_by) == (3, 2, "stop_file")
    assert sleeps == [5, 10]
    alerts = [json.loads(line) for line in (tmp_path / "logs" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [a["level"] for a in alerts] == ["error", "error", "info"] and "disconnected" in alerts[0]["message"]
    assert json.loads((tmp_path / "logs" / "heartbeat.json").read_text(encoding="utf-8"))["state"] == "stopped"


def test_gives_up_after_consecutive_crashes(tmp_path):
    sup, _, sleeps = make(tmp_path, lambda: 1, max_consecutive_crashes=3, base_backoff_s=1, max_backoff_s=2)
    res = sup.run()
    assert res.stopped_by == "max_crashes" and res.crashes == 3 and sleeps == [1, 2]
    hb = json.loads((tmp_path / "logs" / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["state"] == "failed"


def test_deadline(tmp_path):
    sup, clock, _ = make(tmp_path, lambda: 0)
    assert sup.run(until_ms=clock.t).stopped_by == "deadline"


def test_heartbeat_health(tmp_path):
    hb = tmp_path / "hb.json"
    rep = HealthReport()
    check_heartbeat(rep, hb, now_ms=0, max_age_minutes=10)
    assert rep.level == "ok"
    hb.write_text(json.dumps({"ts_ms": 0, "state": "running"}), encoding="utf-8")
    rep = HealthReport()
    check_heartbeat(rep, hb, now_ms=11 * 60_000, max_age_minutes=10)
    assert rep.level == "critical"
    hb.write_text(json.dumps({"ts_ms": 0, "state": "failed"}), encoding="utf-8")
    rep = HealthReport()
    check_heartbeat(rep, hb, now_ms=1, max_age_minutes=10)
    assert rep.level == "critical"
