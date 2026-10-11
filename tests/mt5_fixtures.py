"""Builders for SYNTHETIC files in MT5 export format. Prices are invented; only the format and timing matter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from atlas.brokers.server_time import ServerTimeModel
from atlas.sessions import XauCfd

BAR_HEADER = ["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"]


def server_naive(utc_ms: int, model: ServerTimeModel) -> datetime:
    utc = datetime.fromtimestamp(utc_ms / 1000, timezone.utc)
    return (utc + model.offset_at_utc(utc_ms)).replace(tzinfo=None)


def utc_bar_opens(start_utc_ms: int, end_utc_ms: int, tf_ms: int = 60_000) -> list[int]:
    cal = XauCfd()
    return [t for t in range(start_utc_ms, end_utc_ms, tf_ms) if cal.bar_in_session(t, t + tf_ms)]


def bar_rows(utc_opens: list[int], model: ServerTimeModel, *, price: float = 2400.0) -> list[list[str]]:
    rows = []
    for i, t in enumerate(utc_opens):
        s = server_naive(t, model)
        p = price + (i % 17) * 0.05
        rows.append([s.strftime("%Y.%m.%d"), s.strftime("%H:%M:%S"), f"{p:.2f}", f"{p + 0.40:.2f}",
                     f"{p - 0.30:.2f}", f"{p + 0.10:.2f}", str(50 + i % 40), "0", str(20 + i % 9)])
    return rows


def write_export(path: Path, header: list[str], rows: list[list[str]], *, encoding: str = "utf-16",
                 delimiter: str = "\t") -> Path:
    text = "\r\n".join(delimiter.join(r) for r in [header, *rows]) + "\r\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))  # Python's "utf-16" writes a BOM, as MT5 does
    return path
