"""Generate the SYNTHETIC XAUUSD M5 sample dataset in data/sample/.

The prices are a seeded random walk. They are NOT market data and carry no
information about real gold (XAU/USD) behaviour. The file exists only to exercise the
ingestion and validation pipeline. Three defects are injected deliberately;
see data/sample/README.md for exactly what and where.

Usage: python scripts/generate_sample_data.py
"""

from __future__ import annotations

import csv
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas.sessions import XauCfd  # noqa: E402

SEED = 20261011
TF_MS = 300_000
START = datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc)  # Sunday 18:00 New York (EDT): gold reopens
END = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)  # Friday 17:00 New York (EDT)

GAP_BARS = {f"2026-08-12T10:{m:02d}" for m in range(0, 50, 5)}  # 10:00-10:45 inclusive, 10 bars
MALFORMED_BAR = "2026-08-20T14:00"
BAD_OHLC_BAR = "2026-08-25T09:30"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M")


def bar_sigma(hour: int) -> float:
    # Arbitrary per-bar log-return scale; chosen only so bars look plausible, not calibrated to gold.
    if 12 <= hour < 17:  # London / New York overlap
        return 0.0009
    if 7 <= hour < 12:  # London
        return 0.0006
    return 0.0003


def spread_usd(hour: int, rng: random.Random) -> float:
    if hour in (22, 23):  # just after the daily break spreads are wider
        return rng.uniform(0.40, 0.90)
    return rng.uniform(0.15, 0.35)


def main() -> None:
    rng = random.Random(SEED)
    calendar = XauCfd()
    out = Path(__file__).resolve().parents[1] / "data" / "sample" / "XAUUSD_M5.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    start_ms, end_ms = int(START.timestamp() * 1000), int(END.timestamp() * 1000)

    price = 2400.00  # arbitrary level
    rows = []
    for t in range(start_ms, end_ms, TF_MS):
        if not calendar.bar_in_session(t, t + TF_MS):
            continue
        hour = datetime.fromtimestamp(t / 1000, timezone.utc).hour
        sigma = bar_sigma(hour) / math.sqrt(15)
        o, h, l, c = price, price, price, price
        for _ in range(15):
            c *= math.exp(rng.gauss(0, sigma))
            h, l = max(h, c), min(l, c)
        price = c
        spread = spread_usd(hour, rng)
        stamp = iso(t)
        if stamp in GAP_BARS:
            continue
        bid = [round(v, 2) for v in (o, h, l, c)]
        ask = [round(v + spread, 2) for v in (o, h, l, c)]
        volume = rng.randint(50, 1500)
        row = [stamp + ":00Z", *bid, volume, *ask]
        if stamp == MALFORMED_BAR:
            row[4] = "N/A"
        if stamp == BAD_OHLC_BAR:
            row[2] = round(min(bid[0], bid[3]) - 0.50, 2)  # high below the candle body
        rows.append(row)

    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume", "ask_open", "ask_high", "ask_low", "ask_close"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
