"""Unattended operation: run a job repeatedly, restart it after crashes with capped backoff, write a heartbeat,
stop cleanly on a stop file, and give up (loudly) after too many consecutive crashes.

The supervised job is a PAPER session (`atlas paper-run`); nothing here can place real orders.
Alerts are appended to an alerts file (JSON lines) that `atlas health` and any external notifier can read.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SupervisorResult:
    runs: int
    crashes: int
    stopped_by: str  # "stop_file" | "max_crashes" | "deadline"


def _now_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


class Supervisor:
    def __init__(self, job: Callable[[], int], *, heartbeat: Path, alerts: Path, stop_file: Path,
                 max_consecutive_crashes: int = 5, base_backoff_s: float = 5.0, max_backoff_s: float = 300.0,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep):
        self.job, self.heartbeat, self.alerts, self.stop_file = job, heartbeat, alerts, stop_file
        self.max_crashes, self.base, self.cap = max_consecutive_crashes, base_backoff_s, max_backoff_s
        self.clock, self.sleep = clock, sleep

    def alert(self, level: str, message: str) -> None:
        self.alerts.parent.mkdir(parents=True, exist_ok=True)
        with self.alerts.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(self.clock()), "level": level, "message": message}) + "\n")

    def beat(self, state: str, **extra) -> None:
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.heartbeat.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts_ms": self.clock(), "ts": _now_iso(self.clock()), "state": state, **extra}),
                       encoding="utf-8")
        tmp.replace(self.heartbeat)  # atomic on Windows and POSIX

    def run(self, *, until_ms: int | None = None) -> SupervisorResult:
        runs = crashes = consecutive = 0
        while True:
            if self.stop_file.exists():
                self.beat("stopped", reason="stop_file")
                self.alert("info", "supervisor stopped by stop file")
                return SupervisorResult(runs, crashes, "stop_file")
            if until_ms is not None and self.clock() >= until_ms:
                self.beat("stopped", reason="deadline")
                return SupervisorResult(runs, crashes, "deadline")
            runs += 1
            self.beat("running", run=runs)
            try:
                code = self.job()
                if code not in (0, None):
                    raise RuntimeError(f"job exited with code {code}")
                consecutive = 0
            except KeyboardInterrupt:
                self.beat("stopped", reason="interrupted")
                raise
            except Exception as exc:  # crash: record, back off, retry (capped)
                crashes += 1
                consecutive += 1
                self.alert("error", f"run {runs} crashed ({consecutive} in a row): {type(exc).__name__}: {exc}\n"
                                    + traceback.format_exc(limit=3))
                if consecutive >= self.max_crashes:
                    self.beat("failed", crashes=crashes)
                    self.alert("critical", f"giving up after {consecutive} consecutive crashes; manual attention needed")
                    return SupervisorResult(runs, crashes, "max_crashes")
                delay = min(self.cap, self.base * 2 ** (consecutive - 1))
                self.beat("backoff", delay_s=delay, crashes=crashes)
                self.sleep(delay)
