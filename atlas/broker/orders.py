"""Order preparation: turn an APPROVED risk decision + current quote into a fully validated order request.

This module has no way to send anything. It produces a `PreparedOrder` (or a rejection listing every failed check)
that a simulator - and, only after the owner's explicit authorisation, a future live executor - may consume.

Every check fails closed. The client order id is a deterministic hash of (strategy, symbol, side, decision time),
so the same decision can never produce two different orders (duplicate protection starts here).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum

from ..brokers.spec import BrokerSpec, SpecError, SpecStatus
from ..risk.gate import Check, RiskDecision
from .mt5_readonly import TickSnapshot


class Target(StrEnum):
    PAPER = "paper"  # simulated execution; provisional specs allowed (results labelled)
    # There is deliberately no LIVE target here. Adding one requires the owner's explicit authorisation and a
    # separately reviewed module (docs/14).


ALLOWED_SPEC = {Target.PAPER: {SpecStatus.VERIFIED, SpecStatus.PROVISIONAL_DEV}}
MT5_COMMENT_MAX = 31  # MT5 order comment length limit (to be confirmed on the Vault terminal)


@dataclass(frozen=True)
class PreparedOrder:
    client_order_id: str
    symbol: str
    side: str
    volume: float
    expected_price: float  # ask for buys, bid for sells, at preparation time
    sl: float
    tp: float | None
    deviation_points: int
    magic: int
    comment: str
    decision_time_ms: int
    prepared_at_ms: int
    valid_until_ms: int
    target: Target
    spec_label: str


@dataclass(frozen=True)
class Preparation:
    order: PreparedOrder | None
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return self.order is not None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def client_order_id(strategy_id: str, symbol: str, side: str, decision_time_ms: int) -> str:
    return hashlib.sha256(f"{strategy_id}|{symbol}|{side}|{decision_time_ms}".encode()).hexdigest()[:16]


def _round(price: float, tick: float, direction: int) -> float:
    n = price / tick
    k = math.floor(n + 1e-9) if direction < 0 else math.ceil(n - 1e-9)
    return round(k * tick, 10)


def prepare_order(
    *,
    target: Target,
    spec: BrokerSpec,
    decision: RiskDecision,
    side: str,
    stop_distance: float,
    tp_distance: float | None,
    tick: TickSnapshot,
    quote_time_ms: int,
    now_ms: int,
    strategy_id: str,
    decision_time_ms: int,
    max_quote_age_ms: int = 2_000,
    valid_for_ms: int = 5_000,
    deviation_points: int = 20,
    magic: int = 26_101,
) -> Preparation:
    checks: list[Check] = []

    def check(rule: str, ok: bool, detail: str = "") -> bool:
        checks.append(Check(rule, bool(ok), detail))
        return bool(ok)

    def reject() -> Preparation:
        return Preparation(None, tuple(checks))

    if not check("target", isinstance(target, Target), f"{target!r}"):
        return reject()
    try:
        spec.require(allowed=ALLOWED_SPEC[target])
        check("broker_spec", True, spec.label)
    except SpecError as exc:
        check("broker_spec", False, str(exc))
        return reject()
    s = spec.symbol
    if not check("symbol", tick.symbol == s.name, f"quote {tick.symbol} vs spec {s.name}"):
        return reject()
    check("risk_decision_approved", decision.approved and decision.lots > 0, f"approved={decision.approved}")
    check("side", side in ("buy", "sell"), side)
    check("distances", stop_distance > 0 and (tp_distance is None or tp_distance > 0),
          f"stop={stop_distance} tp={tp_distance}")
    age = now_ms - quote_time_ms
    check("quote_fresh", 0 <= age <= max_quote_age_ms, f"quote age {age} ms")
    check("quote_valid", math.isfinite(tick.bid) and math.isfinite(tick.ask) and 0 < tick.bid <= tick.ask,
          f"bid={tick.bid} ask={tick.ask}")
    if any(not c.passed for c in checks):
        return reject()

    vol = decision.lots
    steps = vol / s.volume_step
    check("volume_step", abs(steps - round(steps)) < 1e-6, f"{vol} vs step {s.volume_step}")
    check("volume_range", s.volume_min - 1e-12 <= vol <= s.volume_max + 1e-12,
          f"{vol} in [{s.volume_min}, {s.volume_max}]")

    tick_size = s.tick_size
    point = 10.0 ** -s.digits
    if side == "buy":
        price = tick.ask
        sl = _round(price - stop_distance, tick_size, -1)          # away from entry
        tp = _round(price + tp_distance, tick_size, -1) if tp_distance else None  # toward entry
        sl_gap, tp_gap = tick.bid - sl, (tp - tick.bid) if tp is not None else None
    else:
        price = tick.bid
        sl = _round(price + stop_distance, tick_size, +1)
        tp = _round(price - tp_distance, tick_size, +1) if tp_distance else None
        sl_gap, tp_gap = sl - tick.ask, (tick.ask - tp) if tp is not None else None
    min_gap = max(s.stops_level_points or 0, s.freeze_level_points or 0) * point
    check("sl_side_and_distance", sl_gap > 0 and sl_gap >= min_gap - 1e-9,
          f"SL {sl} is {sl_gap:.5f} from the closing price; broker minimum {min_gap:.5f}")
    if tp is not None:
        check("tp_side_and_distance", tp_gap > 0 and tp_gap >= min_gap - 1e-9,
              f"TP {tp} is {tp_gap:.5f} from the closing price; broker minimum {min_gap:.5f}")
    check("risk_basis_consistent", abs((price - sl if side == "buy" else sl - price) - stop_distance) <= 2 * tick_size + 1e-9,
          "stop distance used by the risk gate matches the order's SL")
    if any(not c.passed for c in checks):
        return reject()

    cid = client_order_id(strategy_id, s.name, side, decision_time_ms)
    comment = f"atlas:{cid}"[:MT5_COMMENT_MAX]
    return Preparation(PreparedOrder(cid, s.name, side, round(vol, 8), price, sl, tp, deviation_points, magic,
                                     comment, decision_time_ms, now_ms, now_ms + valid_for_ms, target, spec.label),
                       tuple(checks))
