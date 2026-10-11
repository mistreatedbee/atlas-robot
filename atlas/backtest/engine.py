"""Event-driven bar backtester for XAU/USD implementing docs/13_simulation_rules.md v2.

Rule references (R*) point to that document. The engine owns timing, fills, exits, costs and account state;
strategies only decide at bar closes. Every entry passes the deterministic risk gate (R3.3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Protocol

from ..brokers.spec import BrokerSpec
from ..models import Candle, PriceBasis, timeframe_ms
from ..risk.gate import AccountState, CostInputs, Mode, RiskLimits, TradeIntent, evaluate
from ..sessions import XauCfd, is_open_at, trading_day
from .costs import CostModel

MINUTE_MS = 60_000


@dataclass(frozen=True)
class Signal:
    side: str  # "buy" | "sell"
    stop_usd: float  # distance from fill, USD/oz, > 0
    tp_usd: float | None = None


class Strategy(Protocol):
    name: str
    hypothesis_id: str

    def params(self) -> dict: ...

    def prepare(self, candles: Sequence[Candle]) -> None:
        """Precompute CAUSAL features over the full series (verified by the R10 leakage test)."""

    def decide(self, i: int) -> Signal | None:
        """Called at the close of bar i; may use only candles[0..i]."""


@dataclass(frozen=True)
class EngineConfig:
    initial_equity: float
    latency_bars: int = 0
    max_fill_wait_ms: int = 5 * MINUTE_MS
    pre_close_buffer_ms: int = 5 * MINUTE_MS
    max_hold_bars: int | None = None
    kill_switch_engaged: bool = False

    def __post_init__(self):
        if not (math.isfinite(self.initial_equity) and self.initial_equity > 0):
            raise ValueError("initial_equity must be > 0")
        if self.latency_bars < 0 or self.max_fill_wait_ms < 0 or self.pre_close_buffer_ms < 0:
            raise ValueError("latency, fill wait and buffer must be >= 0")
        if self.max_hold_bars is not None and self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")


@dataclass
class Trade:
    side: str
    decision_time: int  # close of the decision bar
    entry_time: int  # fill-bar open
    entry_price: float
    lots: float
    stop: float
    tp: float | None
    planned_risk: float  # account currency, from the risk gate (includes spread + slippage allowance)
    r_unit: float = 0.0  # loss of a clean stop-out from the fill: (|entry-stop| + slippage) x oz x lots + commission
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    held_through_gap: bool = False
    gross_usd: float = 0.0
    commission_usd: float = 0.0
    spread_cost_usd: float = 0.0
    slippage_cost_usd: float = 0.0
    net_usd: float = 0.0
    net_account: float = 0.0
    r_multiple: float = 0.0
    trading_day: str = ""

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Event:
    time: int
    kind: str
    detail: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade]
    events: list[Event]
    equity_curve: list[tuple[int, float]]  # (bar close ms, mark-to-market equity)
    daily: dict[str, list[float]]  # trading day -> [start mtm, end mtm]
    bars_total: int
    bars_in_position: int
    eligible_bars_by_hour: dict[int, int]
    decision_bars_by_hour: dict[int, int]  # in-period bars at whose close the strategy was asked to decide
    initial_equity: float
    usd_to_account: float
    stopped_early: str | None = None


@dataclass
class _Position:
    trade: Trade
    entry_index: int
    entry_spread: float


class _SimKillSwitch:
    def __init__(self, engaged: bool):
        self._engaged = engaged

    def engaged(self) -> bool:
        return self._engaged


def _round_away(price: float, tick: float, direction: int) -> float:
    """Round to tick: direction -1 = down (floor), +1 = up (ceil)."""
    n = price / tick
    k = math.floor(n + 1e-9) if direction < 0 else math.ceil(n - 1e-9)
    return round(k * tick, 10)


def run_backtest(
    candles: Sequence[Candle],
    strategy: Strategy,
    *,
    spec: BrokerSpec,
    limits: RiskLimits,
    costs: CostModel,
    calendar: XauCfd,
    config: EngineConfig,
    trade_start_ms: int | None = None,
    trade_end_ms: int | None = None,
) -> BacktestResult:
    if not candles:
        raise ValueError("no candles")
    tf = timeframe_ms(candles[0].timeframe)
    if candles[0].timeframe not in ("M1", "M5"):
        raise ValueError("the engine is defined for M1/M5 bars (docs/13)")
    for a, b in zip(candles, candles[1:]):
        if b.open_time <= a.open_time:
            raise ValueError("candles must be strictly increasing")
    if any(c.price_basis is not PriceBasis.BID for c in candles):
        raise ValueError("the engine requires BID-based bars (docs/13 R1.1); other price bases would misstate costs")
    sym = spec.symbol
    tick, oz = sym.tick_size, sym.contract_size
    costs.validate(tick)
    fx = costs.usd_to_account
    start = trade_start_ms if trade_start_ms is not None else candles[0].open_time
    end = trade_end_ms if trade_end_ms is not None else candles[-1].close_time
    kill = _SimKillSwitch(config.kill_switch_engaged)
    buffer = config.pre_close_buffer_ms

    strategy.prepare(candles)

    trades: list[Trade] = []
    events: list[Event] = []
    curve: list[tuple[int, float]] = []
    daily: dict[str, list[float]] = {}
    eligible: dict[int, int] = {}
    decision_bars: dict[int, int] = {}
    realised = config.initial_equity
    mtm = realised
    peak = realised
    day_label, day_start, trades_today = None, realised, 0
    position: _Position | None = None
    pending: tuple[int, Signal] | None = None  # (decision bar index, signal)
    bars_in_pos = 0
    stopped = None
    last = len(candles) - 1

    def closure_between(t0: int, t1: int) -> bool:
        return any(not is_open_at(calendar, t) for t in range(t0, t1, MINUTE_MS))

    def close_position(i: int, price: float, reason: str, slip: float) -> None:
        nonlocal position, realised
        tr = position.trade
        tr.exit_time = candles[i].close_time if reason in ("max_hold", "session_close", "end_of_data", "period_end") \
            else candles[i].open_time if reason in ("stop_gap", "tp_gap", "held_through_closure") \
            else candles[i].close_time
        tr.exit_price = round(price, 10)
        tr.exit_reason = reason
        direction = 1 if tr.side == "buy" else -1
        tr.gross_usd = (tr.exit_price - tr.entry_price) * direction * oz * tr.lots
        half_comm = (sym.commission_per_lot_round_turn or 0.0) / 2 * tr.lots
        tr.commission_usd = 2 * half_comm
        exit_spread = costs.spread_at(candles[i], calendar, tick)
        tr.spread_cost_usd = (position.entry_spread if tr.side == "buy" else exit_spread) * oz * tr.lots
        tr.slippage_cost_usd += slip * oz * tr.lots
        tr.net_usd = tr.gross_usd - tr.commission_usd
        tr.net_account = tr.net_usd * fx
        tr.r_multiple = tr.net_account / tr.r_unit if tr.r_unit > 0 else float("nan")
        realised += tr.gross_usd * fx - half_comm * fx  # entry half was charged at entry
        trades.append(tr)
        position = None

    for i, c in enumerate(candles):
        b, e = c.open_time, c.close_time
        spread = costs.spread_at(c, calendar, tick)
        ask_open = max(c.ask_open or 0.0, c.open + spread)
        ask_high = max(c.ask_high or 0.0, c.high + spread)
        ask_low = max(c.ask_low or 0.0, c.low + spread)
        ask_close = max(c.ask_close or 0.0, c.close + spread)
        slip = costs.slippage_usd

        label = trading_day(b)
        in_period = start <= b < end
        if label != day_label:
            day_label, day_start, trades_today = label, mtm, 0
        if in_period and label not in daily:  # warm-up bars never enter the results (finding 11)
            daily[label] = [mtm, mtm]

        # ---- 1. pending order (R2.2-R2.4, R3) -----------------------------------------------------
        if pending is not None:
            j, sig = pending
            if i >= j + 1 + config.latency_bars:
                pending = None
                earliest = candles[j].close_time + config.latency_bars * tf
                if closure_between(candles[j].close_time, b):
                    events.append(Event(b, "expired_closure"))
                elif b > earliest + config.max_fill_wait_ms:
                    events.append(Event(b, "expired_wait", f"hour={(candles[j].close_time // 3_600_000) % 24}"))
                elif not (start <= b < end):
                    events.append(Event(b, "expired_period_end"))
                elif not is_open_at(calendar, b + buffer) or calendar.in_reopen_window(b, costs.reopen_minutes):
                    events.append(Event(b, "blocked_window"))
                else:
                    if sig.side == "buy":
                        fill = ask_open + slip
                        stop = _round_away(fill - sig.stop_usd, tick, -1)
                        tp = _round_away(fill + sig.tp_usd, tick, -1) if sig.tp_usd else None
                    else:
                        fill = c.open - slip
                        stop = _round_away(fill + sig.stop_usd, tick, +1)
                        tp = _round_away(fill - sig.tp_usd, tick, +1) if sig.tp_usd else None
                    fill = round(fill, 10)
                    decision = evaluate(
                        TradeIntent(sig.side, fill, stop, b), spec,
                        AccountState(equity=realised, free_margin=realised, day_start_equity=day_start,
                                     peak_equity=peak, open_risk=0.0, trades_today=trades_today, verified_at_ms=b),
                        limits, CostInputs(spread, slip, fx, costs.sources),
                        now_ms=b, mode=Mode.BACKTEST, kill_switch=kill)
                    if not decision.approved:
                        events.append(Event(b, "risk_rejected", ",".join(ch.rule for ch in decision.failed)))
                    else:
                        tr = Trade(sig.side, candles[j].close_time, b, fill, decision.lots, stop, tp,
                                   decision.risk_account_ccy, trading_day=label)
                        tr.r_unit = ((abs(fill - stop) + slip) * oz * decision.lots
                                     + (sym.commission_per_lot_round_turn or 0.0) * decision.lots) * fx
                        tr.slippage_cost_usd = slip * oz * decision.lots
                        half_comm = (sym.commission_per_lot_round_turn or 0.0) / 2 * decision.lots
                        realised -= half_comm * fx
                        position = _Position(tr, i, spread)
                        trades_today += 1
                        events.append(Event(b, "filled", f"{sig.side} {decision.lots} lots @ {fill}"))

        # ---- 2. price exits (R4.1, R4.2) ------------------------------------------------------------
        if position is not None:
            tr = position.trade
            long = tr.side == "buy"
            if position.entry_index < i:
                prev = candles[i - 1]
                if b != prev.close_time and closure_between(prev.close_time, b):
                    # R4.7: the pre-close session exit never fired (quiet minutes missing before the closure).
                    # Exit at the first bar after the closure at its open: the gap risk is borne, never avoided.
                    tr.held_through_gap = True
                    close_position(i, c.open - slip if long else ask_open + slip, "held_through_closure", slip)
                elif b != prev.close_time:
                    tr.held_through_gap = True  # unexpected data gap held (R4.4)
            if position is not None:
                # R4.1 applies on the entry bar too: a stop inside the spread is hit at the open, never filled
                # better than the market.
                if long and c.open <= tr.stop:
                    close_position(i, c.open - slip, "stop_gap", slip)
                elif long and tr.tp is not None and c.open >= tr.tp:
                    close_position(i, tr.tp, "tp_gap", 0.0)
                elif not long and ask_open >= tr.stop:
                    close_position(i, ask_open + slip, "stop_gap", slip)
                elif not long and tr.tp is not None and ask_open <= tr.tp:
                    close_position(i, tr.tp, "tp_gap", 0.0)
            if position is not None:
                if long:
                    stop_hit = c.low <= tr.stop
                    tp_hit = tr.tp is not None and c.high >= tr.tp + tick - 1e-9
                else:
                    stop_hit = ask_high >= tr.stop
                    tp_hit = tr.tp is not None and ask_low <= tr.tp - tick + 1e-9
                if stop_hit:  # stop first when both are touched
                    close_position(i, tr.stop - slip if long else tr.stop + slip, "stop", slip)
                elif tp_hit:
                    close_position(i, tr.tp, "tp", 0.0)

        # ---- 3. time exits at the close (R4.3) ------------------------------------------------------
        if position is not None:
            tr = position.trade
            reason = None
            if config.max_hold_bars is not None and i - position.entry_index + 1 >= config.max_hold_bars:
                reason = "max_hold"
            elif not is_open_at(calendar, e + buffer):
                reason = "session_close"
            elif i == last:
                reason = "end_of_data"
            elif e >= end:
                reason = "period_end"
            if reason:
                price = c.close - slip if tr.side == "buy" else ask_close + slip
                close_position(i, price, reason, slip)

        # ---- 4. mark to market (R6.2) ---------------------------------------------------------------
        unreal = 0.0
        if position is not None:
            tr = position.trade
            bars_in_pos += in_period
            mark = c.close if tr.side == "buy" else ask_close
            unreal = (mark - tr.entry_price) * (1 if tr.side == "buy" else -1) * oz * tr.lots * fx
        mtm = realised + unreal
        peak = max(peak, mtm)
        if in_period:
            curve.append((e, mtm))
            daily[label][1] = mtm
        if mtm <= 0:
            stopped = "equity_exhausted"
            events.append(Event(e, "stopped", "equity <= 0"))
            break

        # ---- 5. decision at the close (R2.1, R2.5) --------------------------------------------------
        if start <= b and e <= end:
            hour = (e // 3_600_000) % 24
            decision_bars[hour] = decision_bars.get(hour, 0) + 1
            if position is None and pending is None:
                eligible[hour] = eligible.get(hour, 0) + 1
            sig = strategy.decide(i)
            if sig is not None:
                # raw strategy output, logged before the engine's disposition (used by the R10 leakage test)
                events.append(Event(e, "decision", f"{sig.side} stop={sig.stop_usd} tp={sig.tp_usd}"))
                if sig.side not in ("buy", "sell") or not (sig.stop_usd > 0) or (sig.tp_usd is not None and sig.tp_usd <= 0):
                    events.append(Event(e, "invalid_signal", repr(sig)))
                elif position is not None:
                    events.append(Event(e, "ignored_in_position", sig.side))
                elif pending is not None:
                    events.append(Event(e, "ignored_pending", sig.side))
                else:
                    pending = (i, sig)
                    events.append(Event(e, "signal", f"{sig.side} stop={sig.stop_usd} tp={sig.tp_usd}"))

    if not curve:  # nothing in period: keep the starting point so metrics are well-defined
        curve.append((start, mtm))
    return BacktestResult(trades, events, curve, daily, len(curve), bars_in_pos, eligible, decision_bars,
                          config.initial_equity, fx, stopped)
