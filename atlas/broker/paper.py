"""Paper (simulated) execution of PreparedOrders. There is no live counterpart in this codebase.

Safeguards:
* Duplicate orders: the client order id is the PRIMARY KEY of `order_journal`; a second submission of the same id
  is refused even after a restart. Rejected attempts are journalled too, so a rejected decision is never retried
  as if it were new.
* One combined exposure: no new order while a position on the symbol is open.
* Fail closed: kill switch engaged, expired order, stale/invalid quote, price moved beyond the order's deviation,
  or stop-loss / take-profit no longer valid against the current quote -> rejected with the reason recorded.
* Restart recovery: open positions and the journal live in SQLite; a new PaperBroker on the same database
  continues managing them.
* Emergency stop: engages the kill switch and closes every open paper position at market.
Fills: market orders at the current ask (buy) / bid (sell) plus slippage. Stop-loss fills at the worse of the stop
and the current price, plus slippage; take-profit fills at the take-profit (no positive slippage).
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from ..brokers.spec import BrokerSpec
from ..risk.gate import KillSwitch
from ..storage import Store
from .mt5_readonly import TickSnapshot
from .orders import PreparedOrder, Target


class OrderRejected(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class DuplicateOrder(OrderRejected):
    pass


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    client_order_id: str
    symbol: str
    side: str
    volume: float
    entry_price: float
    sl: float
    tp: float | None
    opened_ms: int


@dataclass(frozen=True)
class ClosedPosition:
    position: PaperPosition
    exit_price: float
    exit_reason: str
    closed_ms: int
    gross_usd: float


class PaperBroker:
    def __init__(self, store: Store, spec: BrokerSpec, *, kill_switch: KillSwitch, slippage_usd: float,
                 max_quote_age_ms: int = 2_000):
        if not (math.isfinite(slippage_usd) and slippage_usd >= spec.symbol.tick_size):
            raise ValueError("paper slippage must be at least one tick")
        self.conn = store.conn
        self.spec = spec
        self.kill = kill_switch
        self.slip = slippage_usd
        self.max_quote_age_ms = max_quote_age_ms
        self.point = 10.0 ** -spec.symbol.digits

    # ---- helpers -------------------------------------------------------------------------------------

    def _event(self, t: int, kind: str, detail: str) -> None:
        self.conn.execute("INSERT INTO paper_events (time_ms, kind, detail) VALUES (?,?,?)", (t, kind, detail))

    def _journal(self, order: PreparedOrder, status: str, now_ms: int, reason: str | None = None,
                 fill: float | None = None, position_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO order_journal VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.client_order_id, "paper", order.symbol, order.side, order.volume, status,
             json.dumps(asdict(order), default=str), reason, fill, position_id, now_ms, now_ms))

    def open_positions(self, symbol: str | None = None) -> list[PaperPosition]:
        sql = "SELECT * FROM paper_positions WHERE status='open'" + (" AND symbol=?" if symbol else "")
        rows = self.conn.execute(sql, (symbol,) if symbol else ()).fetchall()
        return [PaperPosition(r["position_id"], r["client_order_id"], r["symbol"], r["side"], r["volume"],
                              r["entry_price"], r["sl"], r["tp"], r["opened_ms"]) for r in rows]

    def journal_status(self, client_order_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM order_journal WHERE client_order_id=?",
                                (client_order_id,)).fetchone()
        return row[0] if row else None

    # ---- submission -----------------------------------------------------------------------------------

    def submit(self, order: PreparedOrder, tick: TickSnapshot, *, quote_time_ms: int, now_ms: int) -> PaperPosition:
        self.conn.execute("BEGIN IMMEDIATE")  # duplicate check + journal + position in one locked transaction
        try:
            position = self._submit_locked(order, tick, quote_time_ms, now_ms)
            self.conn.execute("COMMIT")
            return position
        except DuplicateOrder:
            self.conn.execute("ROLLBACK")  # the original journal row is left untouched
            raise
        except OrderRejected as exc:
            self._journal(order, "rejected", now_ms, reason=str(exc))
            self._event(now_ms, "order_rejected", f"{order.client_order_id} {exc}")
            self.conn.execute("COMMIT")
            raise
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _submit_locked(self, order, tick, quote_time_ms, now_ms) -> PaperPosition:
        if self.journal_status(order.client_order_id) is not None:
            raise DuplicateOrder("duplicate", f"client order id {order.client_order_id} already "
                                              f"{self.journal_status(order.client_order_id)}")
        if self.kill.engaged():
            raise OrderRejected("kill_switch", "engaged")
        if order.target is not Target.PAPER:
            raise OrderRejected("target", f"{order.target!r} is not paper")
        if order.symbol != self.spec.symbol.name or tick.symbol != order.symbol:
            raise OrderRejected("symbol", f"order {order.symbol}, quote {tick.symbol}, spec {self.spec.symbol.name}")
        if now_ms > order.valid_until_ms:
            raise OrderRejected("expired", f"valid until {order.valid_until_ms}, now {now_ms}")
        age = now_ms - quote_time_ms
        if not (0 <= age <= self.max_quote_age_ms):
            raise OrderRejected("stale_quote", f"quote age {age} ms")
        if not (math.isfinite(tick.bid) and math.isfinite(tick.ask) and 0 < tick.bid <= tick.ask):
            raise OrderRejected("invalid_quote", f"bid={tick.bid} ask={tick.ask}")
        if self.open_positions(order.symbol):
            raise OrderRejected("position_open", "one combined exposure per instrument")
        fill = tick.ask + self.slip if order.side == "buy" else tick.bid - self.slip
        if abs(fill - order.expected_price) > order.deviation_points * self.point + 1e-9:
            raise OrderRejected("price_moved", f"fill {fill:.5f} vs expected {order.expected_price:.5f}, "
                                               f"max deviation {order.deviation_points} points")
        if order.side == "buy" and not (order.sl < tick.bid and (order.tp is None or order.tp > tick.bid)):
            raise OrderRejected("levels_invalid", f"SL {order.sl} / TP {order.tp} vs bid {tick.bid}")
        if order.side == "sell" and not (order.sl > tick.ask and (order.tp is None or order.tp < tick.ask)):
            raise OrderRejected("levels_invalid", f"SL {order.sl} / TP {order.tp} vs ask {tick.ask}")
        pos = PaperPosition(str(uuid.uuid4()), order.client_order_id, order.symbol, order.side, order.volume,
                            round(fill, 10), order.sl, order.tp, now_ms)
        self._journal(order, "filled", now_ms, fill=pos.entry_price, position_id=pos.position_id)
        self.conn.execute("INSERT INTO paper_positions (position_id, client_order_id, symbol, side, volume, "
                          "entry_price, sl, tp, opened_ms, status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
                          (pos.position_id, pos.client_order_id, pos.symbol, pos.side, pos.volume, pos.entry_price,
                           pos.sl, pos.tp, pos.opened_ms))
        self._event(now_ms, "filled", f"{pos.client_order_id} {pos.side} {pos.volume} @ {pos.entry_price}")
        return pos

    # ---- position management ---------------------------------------------------------------------------

    def _close(self, pos: PaperPosition, price: float, reason: str, now_ms: int) -> ClosedPosition:
        direction = 1 if pos.side == "buy" else -1
        gross = (price - pos.entry_price) * direction * self.spec.symbol.contract_size * pos.volume
        with self.conn:
            cur = self.conn.execute(
                "UPDATE paper_positions SET status='closed', exit_price=?, exit_reason=?, closed_ms=?, gross_usd=? "
                "WHERE position_id=? AND status='open'", (round(price, 10), reason, now_ms, gross, pos.position_id))
            if cur.rowcount != 1:
                raise RuntimeError(f"position {pos.position_id} was not open")
            self._event(now_ms, "closed", f"{pos.client_order_id} {reason} @ {price:.5f} gross {gross:.2f} USD")
        return ClosedPosition(pos, round(price, 10), reason, now_ms, gross)

    def on_tick(self, tick: TickSnapshot, *, now_ms: int) -> list[ClosedPosition]:
        """Apply stop-loss / take-profit to open positions. Stop first if a single quote crosses both."""
        if not (math.isfinite(tick.bid) and math.isfinite(tick.ask) and 0 < tick.bid <= tick.ask):
            return []  # a bad quote never triggers exits
        closed = []
        for pos in self.open_positions(tick.symbol):
            if pos.side == "buy":
                if tick.bid <= pos.sl:
                    closed.append(self._close(pos, min(pos.sl, tick.bid) - self.slip, "stop", now_ms))
                elif pos.tp is not None and tick.bid >= pos.tp:
                    closed.append(self._close(pos, pos.tp, "tp", now_ms))
            else:
                if tick.ask >= pos.sl:
                    closed.append(self._close(pos, max(pos.sl, tick.ask) + self.slip, "stop", now_ms))
                elif pos.tp is not None and tick.ask <= pos.tp:
                    closed.append(self._close(pos, pos.tp, "tp", now_ms))
        return closed

    def flatten_all(self, tick: TickSnapshot, *, reason: str, now_ms: int) -> list[ClosedPosition]:
        out = []
        for pos in self.open_positions(tick.symbol):
            price = tick.bid - self.slip if pos.side == "buy" else tick.ask + self.slip
            out.append(self._close(pos, price, reason, now_ms))
        return out

    def emergency_stop(self, tick: TickSnapshot | None, *, reason: str, now_ms: int) -> list[ClosedPosition]:
        """Engage the kill switch FIRST (so nothing new can open), then close everything at market if a quote is
        available. Without a valid quote, positions keep their stop-losses and the stop is still recorded."""
        self.kill.engage(f"emergency stop at {now_ms}: {reason}")
        with self.conn:
            self._event(now_ms, "emergency_stop", reason)
        if tick is None or not (0 < tick.bid <= tick.ask):
            return []
        return self.flatten_all(tick, reason=f"emergency: {reason}", now_ms=now_ms)


class ConnectionMonitor:
    """Tracks quote-feed health. New orders are allowed only while connected with a fresh quote; after failures,
    reconnection attempts back off exponentially (capped)."""

    def __init__(self, *, max_quote_age_ms: int = 2_000, base_backoff_ms: int = 1_000, max_backoff_ms: int = 60_000):
        self.max_quote_age_ms = max_quote_age_ms
        self.base, self.cap = base_backoff_ms, max_backoff_ms
        self.failures = 0
        self.last_quote_ms: int | None = None
        self.next_attempt_ms = 0

    def record_quote(self, quote_time_ms: int) -> None:
        self.failures = 0
        self.last_quote_ms = quote_time_ms
        self.next_attempt_ms = 0

    def record_failure(self, now_ms: int) -> int:
        self.failures += 1
        delay = min(self.cap, self.base * 2 ** (self.failures - 1))
        self.next_attempt_ms = now_ms + delay
        return delay

    def may_attempt(self, now_ms: int) -> bool:
        return now_ms >= self.next_attempt_ms

    def can_open_positions(self, now_ms: int) -> bool:
        return (self.failures == 0 and self.last_quote_ms is not None
                and 0 <= now_ms - self.last_quote_ms <= self.max_quote_age_ms)
