"""Live PAPER trading on real MT5 quotes (Windows). Reads through the read-only adapter; executes only in the
paper broker. There is no path from here to a real order.

Each `step()`:
1. polls the current quote (failures -> ConnectionMonitor backoff; nothing is decided without a fresh quote);
2. converts the MT5 server timestamp to UTC with the configured ServerTimeModel and, while the market is open,
   checks it against the local UTC clock. A large difference means the time model (or the PC clock) is wrong:
   the driver stops rather than trade on misplaced sessions / news windows;
3. on a fresh quote: manages stop-loss / take-profit and fills a pending decision (first quote after it);
4. appends newly CLOSED bars (bid OHLC from copy_rates_range) and lets the session decide at each close.
Assumption until verified on the Vault terminal: MT5 returns bar/tick times as server-time epoch seconds.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from ..brokers.server_time import ServerTimeError, ServerTimeModel
from ..models import Candle, PriceBasis, VolumeKind, timeframe_ms
from ..sessions import XauCfd, is_open_at
from .mt5_readonly import BrokerReadError, Mt5ReadOnly, TickSnapshot
from .paper import ConnectionMonitor
from .session import PaperSession

EPOCH = datetime(1970, 1, 1)


def server_epoch_to_utc_ms(seconds: int, model: ServerTimeModel) -> int:
    return model.to_utc_ms(EPOCH + timedelta(seconds=int(seconds)))


class LivePaperDriver:
    def __init__(self, ro: Mt5ReadOnly, session: PaperSession, *, symbol: str, timeframe: str,
                 server_time: ServerTimeModel, monitor: ConnectionMonitor, calendar: XauCfd,
                 clock: Callable[[], int] = lambda: int(_time.time() * 1000),
                 max_clock_skew_ms: int = 120_000, lookback_bars: int = 5):
        if timeframe not in ("M1", "M5"):
            raise ValueError("live paper trading is defined for M1/M5")
        self.ro, self.session, self.symbol, self.timeframe = ro, session, symbol, timeframe
        self.tf = timeframe_ms(timeframe)
        self.model, self.monitor, self.calendar, self.clock = server_time, monitor, calendar, clock
        self.max_clock_skew_ms, self.lookback_bars = max_clock_skew_ms, lookback_bars
        self.clock_verified = False
        self._old_since: int | None = None
        self._last_prices: tuple[float, float] | None = None
        self.log: list[tuple[int, str, str]] = []

    def _refuse(self, now: int, qt: int, why: str) -> None:
        raise ServerTimeError(
            f"{why}: live quote time differs from the local UTC clock by {(qt - now) / 60000:.1f} min. The server-"
            f"time model ({self.model.spec}) or this PC's clock is wrong; refusing to paper trade")

    def _check_clock(self, now: int, qt: int, prices: tuple[float, float]) -> None:
        """A quote stamped in the future is impossible; quotes whose prices keep moving while their timestamps
        stay old for 10+ minutes of open market are equally impossible. Either means misplaced time."""
        if qt - now > self.max_clock_skew_ms:
            self._refuse(now, qt, "quote is stamped in the future")
        if not is_open_at(self.calendar, now):
            self._old_since, self._last_prices = None, prices
            return
        if now - qt <= self.max_clock_skew_ms:
            if not self.clock_verified:
                self.clock_verified = True
                self.log.append((now, "clock_verified", f"skew {(qt - now) / 1000:.1f} s"))
            self._old_since = None
        elif prices != self._last_prices:  # prices are moving but the timestamps are old
            self._old_since = self._old_since or now
            if now - self._old_since > 10 * 60_000:
                self._refuse(now, qt, "prices keep updating but their timestamps stay old")
        self._last_prices = prices

    def _bars_closed_by(self, now_ms: int) -> list[Candle]:
        start = datetime.fromtimestamp((now_ms - self.lookback_bars * self.tf) / 1000, timezone.utc)
        end = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        out = []
        for r in self.ro.rates_range(self.symbol, self.timeframe, start, end):
            open_ms = server_epoch_to_utc_ms(r["time"], self.model)
            if open_ms + self.tf > now_ms:
                continue  # still forming: never decide on an unfinished bar
            out.append(Candle(
                instrument_id="XAUUSD", provider="mt5_live", venue=self.ro.account.server if self.ro.account else "mt5",
                timeframe=self.timeframe, source_symbol=self.symbol, open_time=open_ms, close_time=open_ms + self.tf,
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
                price_basis=PriceBasis.BID, volume=float(r["tick_volume"]), volume_kind=VolumeKind.TICK_COUNT,
                bid_open=float(r["open"]), bid_high=float(r["high"]), bid_low=float(r["low"]),
                bid_close=float(r["close"]), spread_points=int(r["spread"]) if r.get("spread") is not None else None))
        return out

    def step(self) -> None:
        now = self.clock()
        if not self.monitor.may_attempt(now):
            return
        try:
            raw = self.ro.tick(self.symbol)
        except BrokerReadError as exc:
            delay = self.monitor.record_failure(now)
            self.log.append((now, "feed_error", f"{exc}; retry in {delay} ms"))
            return
        qt = server_epoch_to_utc_ms(raw.time_server_s, self.model)
        self._check_clock(now, qt, (raw.bid, raw.ask))
        self.monitor.record_quote(qt)
        fresh = self.monitor.can_open_positions(now)
        if not fresh:
            return  # stale quote (e.g. market closed): no exits, fills or decisions on old prices
        quote = TickSnapshot(self.symbol, raw.time_server_s, raw.bid, raw.ask)
        self.session.on_quote(quote, now)
        if self.session.has_pending():
            self.session.try_fill(qt, quote)
        known = self.session.candles[-1].open_time if self.session.candles else None
        for bar in self._bars_closed_by(now):
            if known is not None and bar.open_time <= known:
                continue
            self.session.candles.append(bar)
            known = bar.open_time
            self.session.close_bar(len(self.session.candles) - 1)

    def run(self, *, until_ms: int, poll_ms: int = 1_000,
            sleep: Callable[[float], None] = _time.sleep, stop: Callable[[], bool] = lambda: False) -> None:
        while self.clock() < until_ms and not stop():
            self.step()
            sleep(poll_ms / 1000)
