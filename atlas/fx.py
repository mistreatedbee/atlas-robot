"""Daily USD/ZAR from ECB euro foreign-exchange reference rates (no credentials), point-in-time.

USD/ZAR = (ZAR per EUR) / (USD per EUR), both ECB reference rates for the same TARGET business day.
The ECB publishes them around 16:00 CET; Atlas treats a day's rate as available only from 17:00 Europe/Berlin that
day (deliberately late), so a simulation at time t never uses a rate published after t.
These are reference rates, not the broker's conversion rate; that difference is a stated limitation.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .http import ReadOnlyHttpClient
from .storage import Store

ECB_HOST = "data-api.ecb.europa.eu"
ECB_URL = f"https://{ECB_HOST}/service/data/EXR/D.{{cur}}.EUR.SP00.A"
BERLIN = ZoneInfo("Europe/Berlin")
SOURCE = "ECB euro foreign exchange reference rates (EXR D.ZAR.EUR / D.USD.EUR)"

DDL = """
CREATE TABLE IF NOT EXISTS fx_rates (
    pair             TEXT NOT NULL,      -- e.g. USDZAR = account currency units per 1 USD
    rate_date        TEXT NOT NULL,      -- ECB reference date (YYYY-MM-DD)
    rate             REAL NOT NULL CHECK (rate > 0),
    available_at_ms  INTEGER NOT NULL,   -- earliest UTC time a simulation may use it
    source           TEXT NOT NULL,
    ingested_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (pair, rate_date)
);
"""


class FxError(RuntimeError):
    pass


def available_at_ms(day: date) -> int:
    return int(datetime.combine(day, time(17, 0), tzinfo=BERLIN).timestamp() * 1000)


def _ecb_series(http: ReadOnlyHttpClient, cur: str, start: date, end: date) -> dict[str, float]:
    data, _ = http.get_json(ECB_URL.format(cur=cur), {"startPeriod": start.isoformat(), "endPeriod": end.isoformat(),
                                                      "format": "jsondata"})
    try:
        series = data["dataSets"][0]["series"]
        (key,) = series.keys()
        dates = data["structure"]["dimensions"]["observation"][0]["values"]
        return {dates[int(k)]["id"]: float(v[0]) for k, v in series[key]["observations"].items() if v and v[0] is not None}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FxError(f"unexpected ECB payload for {cur}: {exc}") from exc


def fetch_usdzar(http: ReadOnlyHttpClient, start: date, end: date) -> list[tuple[str, float]]:
    zar, usd = _ecb_series(http, "ZAR", start, end), _ecb_series(http, "USD", start, end)
    common = sorted(set(zar) & set(usd))
    return [(d, zar[d] / usd[d]) for d in common if zar[d] > 0 and usd[d] > 0]


def store_rates(store: Store, pair: str, rows: list[tuple[str, float]], now_ms: int) -> int:
    store.conn.executescript(DDL)
    with store.conn:
        cur = store.conn.executemany(
            "INSERT OR IGNORE INTO fx_rates VALUES (?,?,?,?,?,?)",
            [(pair, d, r, available_at_ms(date.fromisoformat(d)), SOURCE, now_ms) for d, r in rows])
    return cur.rowcount


@dataclass
class FxSeries:
    pair: str
    times: list[int]  # available_at_ms, ascending
    rates: list[float]
    max_age_ms: int = 5 * 86_400_000  # weekends + holidays; older than this -> refuse

    @classmethod
    def load(cls, store: Store, pair: str = "USDZAR") -> FxSeries:
        store.conn.executescript(DDL)
        rows = store.conn.execute("SELECT available_at_ms, rate FROM fx_rates WHERE pair=? ORDER BY available_at_ms",
                                  (pair,)).fetchall()
        if not rows:
            raise FxError(f"no {pair} rates stored; run `atlas fx-update`")
        return cls(pair, [r[0] for r in rows], [r[1] for r in rows])

    def rate_at(self, t_ms: int) -> float:
        """Latest rate already published at t (point-in-time). Fails closed if none or too old."""
        i = bisect.bisect_right(self.times, t_ms) - 1
        if i < 0:
            raise FxError(f"no {self.pair} rate available at {t_ms}")
        if t_ms - self.times[i] > self.max_age_ms:
            raise FxError(f"{self.pair} rate at {t_ms} is stale ({(t_ms - self.times[i]) / 86_400_000:.1f} days)")
        return self.rates[i]
