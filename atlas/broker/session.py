"""Paper-trading session: bars -> quotes -> strategy -> risk gate -> order preparation -> paper broker.

This is the loop a live-data paper run will use (with the MT5 read-only feed on Windows instead of replayed bars).
Replaying stored bars lets it be tested against the backtester on identical input.

Quote synthesis from a bid bar (labelled simulation): quotes at the open, then along the bar's range towards the
side that hurts an open position first (stop-first, as docs/13 R4.2), then the other extreme, then the close.
Wherever the path crosses an open position's stop-loss or take-profit, an extra quote is emitted exactly at that
level (continuous-price assumption, the same as the backtester's stop/TP fills).

Rules applied here (subset of docs/13, identical meaning): decision at bar close; fill at the next bar's open if
it is contiguous within the fill wait and no scheduled closure lies between; no entries in the pre-close buffer
or reopen window; flat before scheduled closures; one combined exposure; every entry through the risk gate.
Both this session and the backtester (rules v2.4) place stop-loss / take-profit relative to the QUOTE, as a real
order is; the consistency test requires identical decisions, entries, sizes and exits on identical input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..backtest.costs import CostModel
from ..backtest.engine import Strategy
from ..brokers.spec import BrokerSpec
from ..models import Candle, timeframe_ms
from ..risk.gate import AccountState, CostInputs, Mode, RiskLimits, TradeIntent, evaluate
from ..sessions import XauCfd, is_open_at, trading_day
from .mt5_readonly import TickSnapshot
from .orders import Target, prepare_order
from .paper import ClosedPosition, OrderRejected, PaperBroker

MINUTE_MS = 60_000


@dataclass
class SessionReport:
    entries: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    events: list[tuple[int, str, str]] = field(default_factory=list)
    final_equity: float = 0.0


def _round_tick(price: float, tick: float, direction: int) -> float:
    n = price / tick
    k = math.floor(n + 1e-9) if direction < 0 else math.ceil(n - 1e-9)
    return round(k * tick, 10)


def _bid_path(c: Candle, long_position: bool | None) -> list[float]:
    """Bid path through the bar: open -> adverse extreme -> favourable extreme -> close."""
    if long_position is None or long_position:
        return [c.open, c.low, c.high, c.close]
    return [c.open, c.high, c.low, c.close]


def _with_crossings(points: list[float], levels: list[float]) -> list[float]:
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        lo, hi = min(a, b), max(a, b)
        crossed = sorted((x for x in levels if lo < x < hi), reverse=a > b)
        out.extend(crossed)
        out.append(b)
    return out


class PaperSession:
    """Event-driven paper session. The SAME object serves replayed bars (run_paper_session) and live MT5 quotes
    (atlas/broker/live_paper.py):

    * try_fill(fill_time_ms, quote)   - a pending decision may be filled at this quote (next bar open / first live
                                         quote after the decision), subject to closure, wait, window and gate checks;
    * on_quote(quote, now_ms)          - stop-loss / take-profit management on every quote;
    * close_bar(index)                 - the bar `candles[index]` has CLOSED: time exits, mark-to-market, decision.

    `candles` is a list the caller appends closed bars to; the strategy is prepared ONCE with that same list, so
    strategies must only read candles[0..i] in decide(i) (enforced by the leakage tests).
    """

    def __init__(self, strategy: Strategy, *, broker: PaperBroker, spec: BrokerSpec, limits: RiskLimits,
                 costs: CostModel, calendar: XauCfd, initial_equity: float, strategy_id: str,
                 candles: list[Candle], max_fill_wait_ms: int = 5 * MINUTE_MS,
                 pre_close_buffer_ms: int = 5 * MINUTE_MS):
        self.strategy, self.broker, self.spec, self.limits, self.costs = strategy, broker, spec, limits, costs
        self.calendar, self.strategy_id, self.candles = calendar, strategy_id, candles
        self.max_fill_wait_ms, self.pre_close_buffer_ms = max_fill_wait_ms, pre_close_buffer_ms
        self.sym = spec.symbol
        self.fx = costs.usd_to_account
        self.rep = SessionReport()
        self.realised = initial_equity
        self.peak = self.mtm = initial_equity
        self.day_label, self.day_start, self.trades_today = None, initial_equity, 0
        self.pending: tuple[int, object] | None = None
        strategy.prepare(candles)

    # ---- helpers --------------------------------------------------------------------------------------------

    def _closure_between(self, t0: int, t1: int) -> bool:
        return any(not is_open_at(self.calendar, t) for t in range(t0, t1, MINUTE_MS))

    def _book(self, closed: list[ClosedPosition]) -> None:
        for cp in closed:
            self.realised += cp.gross_usd * self.fx
            self.rep.exits.append({"client_order_id": cp.position.client_order_id, "exit_reason": cp.exit_reason,
                                   "exit_price": cp.exit_price, "time": cp.closed_ms, "gross_usd": cp.gross_usd})

    def _roll_day(self, t_ms: int) -> None:
        label = trading_day(t_ms)
        if label != self.day_label:
            self.day_label, self.day_start, self.trades_today = label, self.mtm, 0

    def has_pending(self) -> bool:
        return self.pending is not None

    # ---- events -----------------------------------------------------------------------------------------------

    def try_fill(self, fill_time_ms: int, quote: TickSnapshot) -> None:
        if self.pending is None:
            return
        self._roll_day(fill_time_ms)
        j, sig = self.pending
        self.pending = None
        b, costs, tick_size = fill_time_ms, self.costs, self.sym.tick_size
        decided = self.candles[j].close_time
        rep = self.rep
        if self._closure_between(decided, b):
            rep.events.append((b, "expired_closure", ""))
            return
        if b > decided + self.max_fill_wait_ms:
            rep.events.append((b, "expired_wait", ""))
            return
        if not is_open_at(self.calendar, b + self.pre_close_buffer_ms) or self.calendar.in_reopen_window(
                b, costs.reopen_minutes):
            rep.events.append((b, "blocked_window", ""))
            return
        spread = quote.ask - quote.bid
        entry = quote.ask + costs.slippage_usd if sig.side == "buy" else quote.bid - costs.slippage_usd
        # the order's stop is placed from the QUOTE and rounded away (orders.prepare_order, docs/13 R3.2);
        # the risk gate must size against that exact stop, measured from the expected fill
        stop_price = (_round_tick(quote.ask - sig.stop_usd, tick_size, -1) if sig.side == "buy"
                      else _round_tick(quote.bid + sig.stop_usd, tick_size, +1))
        decision = evaluate(
            TradeIntent(sig.side, round(entry, 10), round(stop_price, 10), b), self.spec,
            AccountState(equity=self.realised, free_margin=self.realised, day_start_equity=self.day_start,
                         peak_equity=self.peak, open_risk=0.0, trades_today=self.trades_today, verified_at_ms=b),
            self.limits, CostInputs(spread, costs.slippage_usd, self.fx, costs.sources),
            now_ms=b, mode=Mode.PAPER, kill_switch=self.broker.kill)
        if not decision.approved:
            rep.events.append((b, "risk_rejected", ",".join(ch.rule for ch in decision.failed)))
            return
        prep = prepare_order(target=Target.PAPER, spec=self.spec, decision=decision, side=sig.side,
                             stop_distance=sig.stop_usd, tp_distance=sig.tp_usd, tick=quote, quote_time_ms=b,
                             now_ms=b, strategy_id=self.strategy_id, decision_time_ms=decided,
                             deviation_points=int(round((costs.slippage_usd / tick_size))) + 1)
        if not prep.ok:
            rep.events.append((b, "prepare_rejected", ",".join(ch.rule for ch in prep.failed)))
            return
        try:
            pos = self.broker.submit(prep.order, quote, quote_time_ms=b, now_ms=b)
        except OrderRejected as exc:
            rep.events.append((b, "order_rejected", exc.reason))
            return
        self.trades_today += 1
        rep.entries.append({"client_order_id": pos.client_order_id, "side": pos.side, "time": b,
                            "decision_time": decided, "lots": pos.volume, "entry_price": pos.entry_price,
                            "sl": pos.sl, "tp": pos.tp})

    def on_quote(self, quote: TickSnapshot, now_ms: int) -> None:
        self._book(self.broker.on_tick(quote, now_ms=now_ms))

    def close_bar(self, i: int, close_quote: TickSnapshot | None = None) -> None:
        c = self.candles[i]
        e = c.close_time
        self._roll_day(c.open_time)
        if close_quote is None:
            spread = self.costs.spread_at(c, self.calendar, self.sym.tick_size)
            close_quote = TickSnapshot(self.sym.name, e // 1000, c.close, round(c.close + spread, 10))
        name = self.sym.name
        if self.broker.open_positions(name) and not is_open_at(self.calendar, e + self.pre_close_buffer_ms):
            self._book(self.broker.flatten_all(close_quote, reason="session_close", now_ms=e))
        unreal = 0.0
        for p in self.broker.open_positions(name):
            mark = close_quote.bid if p.side == "buy" else close_quote.ask
            unreal += (mark - p.entry_price) * (1 if p.side == "buy" else -1) * self.sym.contract_size * p.volume * self.fx
        self.mtm = self.realised + unreal
        self.peak = max(self.peak, self.mtm)
        sig = self.strategy.decide(i)
        if sig is not None:
            if self.broker.open_positions(name):
                self.rep.events.append((e, "ignored_in_position", sig.side))
            elif self.pending is not None:
                self.rep.events.append((e, "ignored_pending", sig.side))
            else:
                self.pending = (i, sig)

    def finish(self, close_quote: TickSnapshot, now_ms: int, reason: str = "end_of_data") -> SessionReport:
        self._book(self.broker.flatten_all(close_quote, reason=reason, now_ms=now_ms))
        self.rep.final_equity = self.realised
        return self.rep


def run_paper_session(
    candles: Sequence[Candle],
    strategy: Strategy,
    *,
    broker: PaperBroker,
    spec: BrokerSpec,
    limits: RiskLimits,
    costs: CostModel,
    calendar: XauCfd,
    initial_equity: float,
    strategy_id: str,
    max_fill_wait_ms: int = 5 * MINUTE_MS,
    pre_close_buffer_ms: int = 5 * MINUTE_MS,
) -> SessionReport:
    """Replay stored bars through a PaperSession (quote path synthesised per bar, see module docstring)."""
    data: list[Candle] = []
    session = PaperSession(strategy, broker=broker, spec=spec, limits=limits, costs=costs, calendar=calendar,
                           initial_equity=initial_equity, strategy_id=strategy_id, candles=data,
                           max_fill_wait_ms=max_fill_wait_ms, pre_close_buffer_ms=pre_close_buffer_ms)
    sym = spec.symbol
    last = len(candles) - 1
    for i, c in enumerate(candles):
        data.append(c)
        b, e = c.open_time, c.close_time
        spread = costs.spread_at(c, calendar, sym.tick_size)
        session.try_fill(b, TickSnapshot(sym.name, b // 1000, c.open, round(max(c.ask_open or 0.0, c.open + spread), 10)))
        open_pos = broker.open_positions(sym.name)
        long_pos = None if not open_pos else open_pos[0].side == "buy"
        levels = []
        if open_pos:
            p = open_pos[0]
            shift = 0.0 if p.side == "buy" else spread  # short levels apply to the ask = bid + spread
            levels = [x - shift for x in (p.sl, p.tp) if x is not None]
        for bid in _with_crossings(_bid_path(c, long_pos), levels):
            session.on_quote(TickSnapshot(sym.name, b // 1000, bid, round(bid + spread, 10)), now_ms=e)
        close_quote = TickSnapshot(sym.name, e // 1000, c.close, round(c.close + spread, 10))
        if i == last:
            session._book(broker.flatten_all(close_quote, reason="end_of_data", now_ms=e))
        session.close_bar(i, close_quote)
    session.rep.final_equity = session.realised
    return session.rep
