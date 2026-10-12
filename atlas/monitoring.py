"""Operational health report for unattended use (`atlas health`; exit code 0 ok / 1 warn / 2 critical).

Checks, per stored candle series and globally:
* data freshness (age of the latest closed bar, if a threshold is configured; market closures are excused);
* most recent ingestion run status (failed/abandoned -> critical), its rejects and invalid bars (warn);
* unresolved source conflicts (warn) and the latest data-quality report status;
* the kill-switch file (engaged -> critical, reported so nobody is surprised that nothing trades);
* the most recent experiment (failed -> warn).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .risk.gate import KillSwitch
from .sessions import get_calendar
from .storage import Store

LEVELS = {"ok": 0, "warn": 1, "critical": 2}


@dataclass
class Finding:
    level: str
    check: str
    detail: str


@dataclass
class HealthReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, check: str, detail: str) -> None:
        self.findings.append(Finding(level, check, detail))

    @property
    def level(self) -> str:
        return max((f.level for f in self.findings), key=LEVELS.__getitem__, default="ok")

    @property
    def exit_code(self) -> int:
        return LEVELS[self.level]


def _calendar_minutes_open(calendar, start_ms: int, end_ms: int) -> int:
    """Minutes the market was open in [start, end): staleness only counts trading time."""
    step = 60_000
    return sum(1 for t in range(start_ms, end_ms, step) if calendar.bar_in_session(t, t + step))


def check_heartbeat(rep: HealthReport, heartbeat_path, now_ms: int, max_age_minutes: float) -> None:
    """Unattended operation: the supervisor's heartbeat must be recent while it claims to be running."""
    from pathlib import Path
    path = Path(heartbeat_path)
    if not path.exists():
        rep.add("ok", "heartbeat", "no supervisor heartbeat (not running unattended)")
        return
    try:
        hb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        rep.add("critical", "heartbeat", f"unreadable heartbeat: {exc}")
        return
    age = (now_ms - int(hb.get("ts_ms", 0))) / 60_000
    state = hb.get("state")
    if state == "failed":
        rep.add("critical", "heartbeat", "supervisor gave up after repeated crashes (see logs/alerts.jsonl)")
    elif state in ("running", "backoff") and age > max_age_minutes:
        rep.add("critical", "heartbeat", f"supervisor state '{state}' but last heartbeat {age:.1f} min ago")
    else:
        rep.add("ok", "heartbeat", f"supervisor {state}, {age:.1f} min ago")


def check_health(store: Store, *, now_ms: int, kill_switch: KillSwitch, calendars: dict[str, str],
                 max_data_age_minutes: float | None = None) -> HealthReport:
    rep = HealthReport()
    conn = store.conn

    series = conn.execute(
        """SELECT instrument_id, provider, venue, timeframe, MAX(close_time_utc) AS last_close, COUNT(*) AS n,
                  GROUP_CONCAT(DISTINCT data_label) AS labels
           FROM candles GROUP BY instrument_id, provider, venue, timeframe""").fetchall()
    if not series:
        rep.add("warn", "data", "no candles stored")
    for s in series:
        key = f"{s['instrument_id']}/{s['provider']}/{s['venue']}/{s['timeframe']}"
        if max_data_age_minutes is not None and "synthetic_sample" not in s["labels"]:
            cal = get_calendar(calendars.get(s["instrument_id"], "xau_cfd"))
            open_minutes = _calendar_minutes_open(cal, s["last_close"], now_ms)
            level = "critical" if open_minutes > max_data_age_minutes else "ok"
            rep.add(level, "freshness", f"{key}: {open_minutes} trading minutes since last closed bar "
                                        f"(threshold {max_data_age_minutes})")
        run = conn.execute(
            """SELECT * FROM ingestion_runs WHERE instrument_id=? AND provider=? AND venue=? AND timeframe=?
               ORDER BY started_at_utc DESC, rowid DESC LIMIT 1""",
            (s["instrument_id"], s["provider"], s["venue"], s["timeframe"])).fetchone()
        if run is not None:
            if run["status"] in ("failed", "abandoned"):
                rep.add("critical", "ingestion", f"{key}: last run {run['run_id'][:8]} {run['status']}: {run['error']}")
            elif run["status"] == "running":
                rep.add("warn", "ingestion", f"{key}: run {run['run_id'][:8]} still marked running")
            else:
                rep.add("ok", "ingestion", f"{key}: last run {run['run_id'][:8]} succeeded")
            if run["rejected"] or run["invalid"]:
                rep.add("warn", "ingestion", f"{key}: last run rejected {run['rejected']} rows, "
                                             f"{run['invalid']} invalid bars (see rejected_records / candles)")
        q = conn.execute(
            """SELECT status, summary_json FROM quality_reports WHERE instrument_id=? AND provider=? AND venue=?
               AND timeframe=? ORDER BY created_at_utc DESC, rowid DESC LIMIT 1""",
            (s["instrument_id"], s["provider"], s["venue"], s["timeframe"])).fetchone()
        if q is not None:
            summary = json.loads(q["summary_json"])
            level = {"valid": "ok", "warning": "warn"}.get(q["status"], "critical")
            rep.add(level, "quality", f"{key}: latest quality report {q['status']} "
                                      f"({summary.get('unexpected_gap_count', 0)} unexpected gaps)")

    conflicts = store.count("candle_conflicts")
    if conflicts:
        rep.add("warn", "conflicts", f"{conflicts} source revisions recorded in candle_conflicts (review needed)")

    if kill_switch.engaged():
        rep.add("critical", "kill_switch", f"engaged ({kill_switch.path}); all simulated entries are rejected")
    else:
        rep.add("ok", "kill_switch", "clear")

    exp = conn.execute("SELECT experiment_id, status, error FROM experiments "
                       "ORDER BY created_at_utc DESC, rowid DESC LIMIT 1").fetchone()
    if exp is not None:
        if exp["status"] == "failed":
            rep.add("warn", "experiments", f"latest experiment {exp['experiment_id'][:8]} failed: {exp['error']}")
        elif exp["status"] == "running":
            rep.add("warn", "experiments", f"experiment {exp['experiment_id'][:8]} still running or crashed")
        else:
            rep.add("ok", "experiments", f"latest experiment {exp['experiment_id'][:8]} {exp['status']}")
    return rep
