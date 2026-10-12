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
    tf = timeframe_ms(candles[0].timeframe)
    sym = spec.symbol
    oz, tick_size = sym.contract_size, sym.tick_size
    fx = costs.usd_to_account
    rep = SessionReport()
    realised = initial_equity
    peak = mtm = realised
    day_label, day_start, trades_today = None, realised, 0
    pending: tuple[int, object] | None = None
    strategy.prepare(candles)

    def closure_between(t0: int, t1: int) -> bool:
        return any(not is_open_at(calendar, t) for t in range(t0, t1, MINUTE_MS))

    def book(closed: list[ClosedPosition]) -> None:
        nonlocal realised
        for cp in closed:
            realised += cp.gross_usd * fx
            rep.exits.append({"client_order_id": cp.position.client_order_id, "exit_reason": cp.exit_reason,
                              "exit_price": cp.exit_price, "time": cp.closed_ms, "gross_usd": cp.gross_usd})

    for i, c in enumerate(candles):
        b, e = c.open_time, c.close_time
        spread = costs.spread_at(c, calendar, tick_size)
        label = trading_day(b)
        if label != day_label:
            day_label, day_start, trades_today = label, mtm, 0

        # ---- entry at this bar's open for a decision made at the previous close ------------------------
        if pending is not None:
            j, sig = pending
            pending = None
            if closure_between(candles[j].close_time, b):
                rep.events.append((b, "expired_closure", ""))
            elif b > candles[j].close_time + max_fill_wait_ms:
                rep.events.append((b, "expired_wait", ""))
            elif not is_open_at(calendar, b + pre_close_buffer_ms) or calendar.in_reopen_window(b, costs.reopen_minutes):
                rep.events.append((b, "blocked_window", ""))
            else:
                quote = TickSnapshot(sym.name, b // 1000, c.open, round(max(c.ask_open or 0.0, c.open + spread), 10))
                entry = quote.ask + costs.slippage_usd if sig.side == "buy" else quote.bid - costs.slippage_usd
                # the order's stop is placed from the QUOTE and rounded away (orders.prepare_order, docs/13 R3.2);
                # the risk gate must size against that exact stop, measured from the expected fill
                stop_price = (_round_tick(quote.ask - sig.stop_usd, tick_size, -1) if sig.side == "buy"
                              else _round_tick(quote.bid + sig.stop_usd, tick_size, +1))
                decision = evaluate(
                    TradeIntent(sig.side, round(entry, 10), round(stop_price, 10), b), spec,
                    AccountState(equity=realised, free_margin=realised, day_start_equity=day_start, peak_equity=peak,
                                 open_risk=0.0, trades_today=trades_today, verified_at_ms=b),
                    limits, CostInputs(spread, costs.slippage_usd, fx, costs.sources),
                    now_ms=b, mode=Mode.PAPER, kill_switch=broker.kill)
                if not decision.approved:
                    rep.events.append((b, "risk_rejected", ",".join(ch.rule for ch in decision.failed)))
                else:
                    prep = prepare_order(target=Target.PAPER, spec=spec, decision=decision, side=sig.side,
                                         stop_distance=sig.stop_usd, tp_distance=sig.tp_usd, tick=quote,
                                         quote_time_ms=b, now_ms=b, strategy_id=strategy_id,
                                         decision_time_ms=candles[j].close_time,
                                         deviation_points=int(round((costs.slippage_usd / tick_size))) + 1)
                    if not prep.ok:
                        rep.events.append((b, "prepare_rejected", ",".join(ch.rule for ch in prep.failed)))
                    else:
                        try:
                            pos = broker.submit(prep.order, quote, quote_time_ms=b, now_ms=b)
                            trades_today += 1
                            rep.entries.append({"client_order_id": pos.client_order_id, "side": pos.side,
                                                "time": b, "decision_time": candles[j].close_time,
                                                "lots": pos.volume, "entry_price": pos.entry_price,
                                                "sl": pos.sl, "tp": pos.tp})
                        except OrderRejected as exc:
                            rep.events.append((b, "order_rejected", exc.reason))

        # ---- price path through the bar: stop-loss / take-profit -----------------------------------------
        open_pos = broker.open_positions(sym.name)
        long_pos = None if not open_pos else open_pos[0].side == "buy"
        levels = []
        if open_pos:
            p = open_pos[0]
            shift = 0.0 if p.side == "buy" else spread  # short levels apply to the ask = bid + spread
            levels = [x - shift for x in (p.sl, p.tp) if x is not None]
        for bid in _with_crossings(_bid_path(c, long_pos), levels):
            book(broker.on_tick(TickSnapshot(sym.name, b // 1000, bid, round(bid + spread, 10)), now_ms=e))

        # ---- time exits at the close ---------------------------------------------------------------------
        close_quote = TickSnapshot(sym.name, e // 1000, c.close, round(c.close + spread, 10))
        if broker.open_positions(sym.name):
            if not is_open_at(calendar, e + pre_close_buffer_ms):
                book(broker.flatten_all(close_quote, reason="session_close", now_ms=e))
            elif i == len(candles) - 1:
                book(broker.flatten_all(close_quote, reason="end_of_data", now_ms=e))

        # ---- mark to market --------------------------------------------------------------------------------
        unreal = 0.0
        for p in broker.open_positions(sym.name):
            mark = c.close if p.side == "buy" else close_quote.ask
            unreal += (mark - p.entry_price) * (1 if p.side == "buy" else -1) * oz * p.volume * fx
        mtm = realised + unreal
        peak = max(peak, mtm)

        # ---- decision at the close -------------------------------------------------------------------------
        sig = strategy.decide(i)
        if sig is not None:
            if broker.open_positions(sym.name):
                rep.events.append((e, "ignored_in_position", sig.side))
            elif pending is not None:
                rep.events.append((e, "ignored_pending", sig.side))
            else:
                pending = (i, sig)
    rep.final_equity = realised
    return rep
