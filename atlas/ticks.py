"""Broker tick import (bid/ask) and the measured spread profile that replaces provisional spread assumptions.

Import rule: a tick export (typically Mon-Fri) has no weekend gap to verify the server time zone, so the server-time
model must already have passed verification on a BAR export from the same provider/venue (ingestion run with
server_time_status 'consistent_unconfirmed' or 'verified' and the same model). Otherwise the import is refused.

Spread statistics are TIME-WEIGHTED: each quote's spread counts for as long as it was the live quote (capped at
60 s, so closures and quiet gaps don't dominate). This answers "what spread does an entry at a random moment pay",
which is what the backtester needs - tick-weighted figures over-represent busy moments.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .brokers.server_time import ServerTimeModel
from .events import EventCalendar
from .providers.mt5_export import Tick, _decode, parse_ticks
from .sessions import NEW_YORK, XauCfd
from .storage import Store

DURATION_CAP_MS = 60_000


class TickImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class TickImport:
    import_id: str
    rows_imported: int
    rows_rejected: int
    duplicates_ignored: int
    already_imported: bool
    basis_run_id: str


def _verified_basis(store: Store, instrument_id: str, provider: str, venue: str, model: str) -> str:
    rows = store.conn.execute(
        "SELECT run_id, source_detail FROM ingestion_runs WHERE instrument_id=? AND provider=? AND venue=? "
        "AND source_detail IS NOT NULL ORDER BY started_at_utc DESC", (instrument_id, provider, venue)).fetchall()
    for r in rows:
        d = json.loads(r["source_detail"])
        if d.get("server_time_model") == model and d.get("server_time_status") in ("consistent_unconfirmed", "verified"):
            return r["run_id"]
    raise TickImportError(
        f"no bar import from {provider}/{venue} has verified server-time model {model!r}; import a bar export that "
        "spans a weekend first (docs/10), so the tick timestamps can be placed in UTC")


def import_ticks(store: Store, path: str | Path, *, instrument_id: str, provider: str, venue: str,
                 server_time_model: str, now_ms: int) -> TickImport:
    path = Path(path)
    basis = _verified_basis(store, instrument_id, provider, venue, server_time_model)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    prior = store.conn.execute("SELECT * FROM tick_imports WHERE provider=? AND venue=? AND file_sha256=?",
                               (provider, venue, sha)).fetchone()
    if prior is not None:
        return TickImport(prior["import_id"], 0, 0, 0, True, prior["server_time_basis"])
    ticks, rejects = parse_ticks(_decode(data), path.name, ServerTimeModel(server_time_model))
    import_id = str(uuid.uuid4())
    rows, seq, last_t = [], 0, None
    for t in ticks:
        seq = seq + 1 if t.time_ms == last_t else 0
        last_t = t.time_ms
        rows.append((instrument_id, provider, venue, t.time_ms, seq, t.bid, t.ask, import_id))
    with store.conn:
        store.conn.execute(
            "INSERT INTO tick_imports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (import_id, instrument_id, provider, venue, path.name, sha, server_time_model, basis, 0, len(rejects),
             ticks[0].time_ms if ticks else None, ticks[-1].time_ms if ticks else None, now_ms))
        before = store.conn.total_changes
        store.conn.executemany("INSERT OR IGNORE INTO ticks VALUES (?,?,?,?,?,?,?,?)", rows)
        inserted = store.conn.total_changes - before
        store.conn.execute("UPDATE tick_imports SET rows_imported=? WHERE import_id=?", (inserted, import_id))
    return TickImport(import_id, inserted, len(rejects), len(rows) - inserted, False, basis)


def load_ticks(store: Store, instrument_id: str, provider: str, venue: str, start_ms: int, end_ms: int) -> list[Tick]:
    return [Tick(r[0], r[1], r[2]) for r in store.conn.execute(
        "SELECT time_ms, bid, ask FROM ticks WHERE instrument_id=? AND provider=? AND venue=? AND time_ms>=? "
        "AND time_ms<? ORDER BY time_ms, seq", (instrument_id, provider, venue, start_ms, end_ms))]


def _weighted_percentiles(pairs: list[tuple[float, float]], qs=(0.5, 0.9, 0.99)) -> dict[str, float]:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    out = {}
    for q in qs:
        target, acc = q * total, 0.0
        for v, w in pairs:
            acc += w
            if acc >= target - 1e-12:
                out[f"p{int(q * 100)}"] = v
                break
    return out


def spread_profile(ticks: list[Tick], *, calendar: XauCfd, reopen_minutes: int = 15,
                   events: EventCalendar | None = None, news_window_ms: int = 15 * 60_000) -> dict:
    """Time-weighted spread (USD/oz) by New York hour, in the reopen window, around known news, and 'normal'."""
    buckets: dict[str, list[tuple[float, float]]] = {}
    for a, b in zip(ticks, ticks[1:] + [None]):
        dur = DURATION_CAP_MS if b is None else min(b.time_ms - a.time_ms, DURATION_CAP_MS)
        if dur <= 0:
            continue
        spread = round(a.ask - a.bid, 10)
        ny_hour = datetime.fromtimestamp(a.time_ms / 1000, timezone.utc).astimezone(NEW_YORK).hour
        keys = ["all", f"ny_hour_{ny_hour:02d}"]
        reopen = calendar.in_reopen_window(a.time_ms, reopen_minutes)
        news = events is not None and events.window_event(a.time_ms, before_ms=news_window_ms, after_ms=news_window_ms,
                                                          as_of_ms=a.time_ms) is not None
        if reopen:
            keys.append("reopen_window")
        if news:
            keys.append("news_window")
        if not reopen and not news:
            keys.append("normal")
        for k in keys:
            buckets.setdefault(k, []).append((spread, dur))
    out = {}
    for k, pairs in sorted(buckets.items()):
        stats = _weighted_percentiles(pairs)
        stats["minutes"] = round(sum(w for _, w in pairs) / 60_000, 2)
        stats["quotes"] = len(pairs)
        stats["max"] = max(v for v, _ in pairs)
        out[k] = stats
    return out


def _ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 10)


def suggested_costs(profile: dict, *, tick_size: float, source: str) -> dict:
    """Spread inputs for the cost model from MEASURED broker spreads (p90 = conservative). Slippage is not
    observable from quotes and stays a separate, provisional input."""
    if "normal" not in profile:
        raise ValueError("profile has no 'normal' period to base the spread on")
    base = _ceil_tick(profile["normal"]["p90"], tick_size)
    reopen = _ceil_tick(max(profile.get("reopen_window", {}).get("p90", base), base), tick_size)
    return {"base_spread_usd": base, "reopen_spread_usd": reopen,
            "basis": "time-weighted p90 of measured spreads", "source": source,
            "minutes_measured": profile["normal"]["minutes"],
            "note": "slippage is not measurable from quotes; keep it provisional until demo fills are recorded"}


def bar_spread_semantics(bars: list[dict], ticks: list[Tick], point: float) -> dict:
    """Which tick statistic does MT5's bar <SPREAD> column match? Returns mean absolute error (points) per candidate
    over bars covered by ticks: 'min', 'max', 'mean' (time-weighted), 'open', 'close'."""
    errors: dict[str, list[float]] = {k: [] for k in ("min", "max", "mean", "open", "close")}
    i = 0
    for bar in bars:
        if bar.get("spread_points") is None:
            continue
        inside = []
        while i < len(ticks) and ticks[i].time_ms < bar["open_time"]:
            i += 1
        j = i
        while j < len(ticks) and ticks[j].time_ms < bar["close_time"]:
            inside.append(ticks[j])
            j += 1
        if not inside:
            continue
        sp = [(t.ask - t.bid) / point for t in inside]
        durs = [max(1, min(((inside[k + 1].time_ms if k + 1 < len(inside) else bar["close_time"]) - inside[k].time_ms),
                           DURATION_CAP_MS)) for k in range(len(inside))]
        cand = {"min": min(sp), "max": max(sp), "mean": sum(s * d for s, d in zip(sp, durs)) / sum(durs),
                "open": sp[0], "close": sp[-1]}
        for k, v in cand.items():
            errors[k].append(abs(v - bar["spread_points"]))
    mae = {k: (sum(v) / len(v) if v else None) for k, v in errors.items()}
    known = {k: v for k, v in mae.items() if v is not None}
    return {"bars_compared": len(errors["min"]), "mae_points": mae,
            "best_match": min(known, key=known.get) if known else None}
