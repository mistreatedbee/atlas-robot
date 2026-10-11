"""Pre-trade risk gate and position sizing for XAU/USD.

Design rules:
* Deterministic and fail-closed: any missing, stale, non-finite or unverified input rejects the trade.
* Every check is evaluated and reported, so a rejection explains itself.
* Position size depends only on current equity, limits, stop distance and costs. It takes no loss history,
  so loss-recovery sizing (martingale, averaging down) is structurally impossible.
* All open XAUUSD positions are ONE exposure: open risk is shared across trades.
* If even the minimum broker volume would exceed the risk limit, the trade is rejected, never rounded up.
* There is no live mode. Modes are BACKTEST and PAPER (simulated) only.
* Configured limits can never exceed hard ceilings defined here (changing those requires a code change + review).
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..brokers.spec import BrokerSpec, SpecError, SpecStatus


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"


ALLOWED_SPEC_STATUS = {
    Mode.BACKTEST: {SpecStatus.VERIFIED, SpecStatus.PROVISIONAL_DEV},
    Mode.PAPER: {SpecStatus.VERIFIED, SpecStatus.PROVISIONAL_DEV},
}

HARD_CEILINGS = {
    "max_risk_per_trade_fraction": 0.02,
    "max_open_risk_fraction": 0.02,
    "max_daily_loss_fraction": 0.05,
    "max_drawdown_fraction": 0.25,
    "max_margin_utilisation": 0.5,
}


class RiskConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_fraction: float
    max_open_risk_fraction: float
    max_daily_loss_fraction: float
    max_drawdown_fraction: float
    max_trades_per_day: int
    max_price_age_ms: int
    max_account_state_age_ms: int
    max_margin_utilisation: float

    def __post_init__(self):
        for name, ceiling in HARD_CEILINGS.items():
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and 0 < v <= ceiling):
                raise RiskConfigError(f"{name}={v} must be in (0, {ceiling}] (hard ceiling)")
        for name in ("max_trades_per_day", "max_price_age_ms", "max_account_state_age_ms"):
            v = getattr(self, name)
            if not (isinstance(v, int) and v > 0):
                raise RiskConfigError(f"{name}={v} must be a positive integer")

    @classmethod
    def load(cls, path: str | Path) -> RiskLimits:
        with Path(path).open("rb") as fh:
            data = tomllib.load(fh)
        try:
            return cls(**data["limits"])
        except (KeyError, TypeError) as exc:
            raise RiskConfigError(f"{path}: invalid [limits]: {exc}") from None


class KillSwitch:
    """File-based kill switch. Engaged if the file exists OR its state cannot be determined (fail closed)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def engaged(self) -> bool:
        try:
            return self.path.exists()
        except OSError:
            return True

    def engage(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason)


@dataclass(frozen=True)
class AccountState:
    """Snapshot of account state in the ACCOUNT currency (ZAR for the target account)."""

    equity: float
    free_margin: float
    day_start_equity: float
    peak_equity: float
    open_risk: float  # sum of risk-to-stop of all open XAUUSD positions
    trades_today: int
    verified_at_ms: int  # when this state was last confirmed (broker or simulator)


@dataclass(frozen=True)
class CostInputs:
    """Per-trade costs in USD per ounce, plus the USD->account-currency rate. Every value carries a source."""

    spread_usd: float
    slippage_usd: float
    usd_to_account: float
    sources: tuple[str, ...]


@dataclass(frozen=True)
class TradeIntent:
    side: str  # "buy" | "sell"
    entry_price: float
    stop_price: float
    price_time_ms: int


@dataclass(frozen=True)
class Check:
    rule: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    lots: float
    risk_account_ccy: float
    risk_fraction: float
    checks: tuple[Check, ...]
    mode: Mode
    spec_label: str
    cost_sources: tuple[str, ...]

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def _finite_positive(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in values)


def loss_per_lot(spec: BrokerSpec, stop_distance: float, costs: CostInputs) -> float:
    """Account-currency loss of 1.0 lot stopped out, including spread, slippage and round-turn commission."""
    s = spec.symbol
    per_oz = stop_distance + costs.spread_usd + costs.slippage_usd
    commission_usd = s.commission_per_lot_round_turn or 0.0
    if commission_usd and s.commission_currency not in (None, "USD"):
        raise SpecError(f"commission currency {s.commission_currency} not supported yet")
    return (per_oz * s.contract_size + commission_usd) * costs.usd_to_account


def evaluate(
    intent: TradeIntent,
    spec: BrokerSpec,
    account: AccountState,
    limits: RiskLimits,
    costs: CostInputs,
    *,
    now_ms: int,
    mode: Mode,
    kill_switch: KillSwitch | None,
) -> RiskDecision:
    checks: list[Check] = []

    def check(rule: str, passed: bool, detail: str = "") -> bool:
        checks.append(Check(rule, bool(passed), detail))
        return bool(passed)

    def reject() -> RiskDecision:
        return RiskDecision(False, 0.0, 0.0, 0.0, tuple(checks), mode, spec.label, costs.sources)

    if not isinstance(mode, Mode):  # there is no live mode; anything else is refused
        check("mode", False, f"unsupported mode {mode!r}")
        return reject()
    check("mode", True, mode.value)
    engaged = True if kill_switch is None else kill_switch.engaged()
    check("kill_switch", not engaged, "engaged or not provided" if engaged else "clear")

    try:
        spec.require(allowed=ALLOWED_SPEC_STATUS[mode])
        check("broker_spec", True, spec.label)
    except SpecError as exc:
        check("broker_spec", False, str(exc))
        return reject()
    s = spec.symbol
    if not check("account_leverage_known", spec.account.leverage is not None and spec.account.leverage > 0,
                 f"leverage={spec.account.leverage}"):
        return reject()

    inputs_ok = check(
        "inputs_finite",
        _finite_positive(intent.entry_price, intent.stop_price, account.equity, account.day_start_equity,
                         account.peak_equity, costs.usd_to_account)
        and all(math.isfinite(v) and v >= 0 for v in (costs.spread_usd, costs.slippage_usd, account.open_risk,
                                                       account.free_margin)),
        "prices, equity, FX rate and costs must be finite; prices/equity/FX > 0",
    )
    check("costs_sourced", bool(costs.sources), "every cost input must state its source")
    if not inputs_ok:
        return reject()

    price_age = now_ms - intent.price_time_ms
    check("price_fresh", 0 <= price_age <= limits.max_price_age_ms, f"price age {price_age} ms")
    state_age = now_ms - account.verified_at_ms
    check("account_state_fresh", 0 <= state_age <= limits.max_account_state_age_ms, f"state age {state_age} ms")

    if intent.side == "buy":
        distance = intent.entry_price - intent.stop_price
    elif intent.side == "sell":
        distance = intent.stop_price - intent.entry_price
    else:
        check("side", False, f"unknown side {intent.side!r}")
        return reject()
    check("stop_on_correct_side", distance > 0, f"stop distance {distance:.5f} USD/oz")
    min_distance = s.stops_level_points * s.tick_size
    check("stop_distance_vs_stops_level", distance >= min_distance and distance > 0,
          f"{distance:.5f} >= broker minimum {min_distance:.5f}")

    daily_loss = account.day_start_equity - account.equity
    check("daily_loss_limit", daily_loss < limits.max_daily_loss_fraction * account.day_start_equity,
          f"day loss {daily_loss:.2f} vs limit {limits.max_daily_loss_fraction * account.day_start_equity:.2f}")
    drawdown = account.peak_equity - account.equity
    check("max_drawdown", drawdown < limits.max_drawdown_fraction * account.peak_equity,
          f"drawdown {drawdown:.2f} vs limit {limits.max_drawdown_fraction * account.peak_equity:.2f}")
    check("trade_frequency", account.trades_today < limits.max_trades_per_day,
          f"{account.trades_today} trades today, max {limits.max_trades_per_day}")

    if any(not c.passed for c in checks) or distance <= 0:
        return reject()

    per_lot = loss_per_lot(spec, distance, costs)
    budget = min(limits.max_risk_per_trade_fraction * account.equity,
                 limits.max_open_risk_fraction * account.equity - account.open_risk)
    if not check("open_risk_capacity", budget > 0, f"remaining risk budget {budget:.2f}"):
        return reject()
    steps = math.floor(budget / per_lot / s.volume_step + 1e-9)
    lots = round(min(steps * s.volume_step, s.volume_max), 8)
    min_risk = per_lot * s.volume_min
    if not check("minimum_volume_within_risk_limit", lots >= s.volume_min - 1e-12,
                 f"min volume {s.volume_min} lot risks {min_risk:.2f} = {min_risk / account.equity:.2%} of equity; "
                 f"budget {budget:.2f}"):
        return reject()

    margin = lots * s.contract_size * intent.entry_price / spec.account.leverage * costs.usd_to_account
    if not check("margin", margin <= limits.max_margin_utilisation * account.free_margin,
                 f"margin {margin:.2f} vs {limits.max_margin_utilisation:.0%} of free margin {account.free_margin:.2f}"):
        return reject()

    risk = per_lot * lots
    return RiskDecision(True, lots, risk, risk / account.equity, tuple(checks), mode, spec.label, costs.sources)


@dataclass(frozen=True)
class FeasibilityRow:
    stop_usd: float
    risk_at_min_volume: float
    risk_fraction_at_min_volume: float
    permitted: bool


def feasibility(spec: BrokerSpec, equity: float, stops_usd: list[float], costs: CostInputs,
                limits: RiskLimits) -> list[FeasibilityRow]:
    """Risk of the smallest possible trade at each stop distance, vs the per-trade limit."""
    spec.require(allowed={SpecStatus.VERIFIED, SpecStatus.PROVISIONAL_DEV})
    rows = []
    for stop in stops_usd:
        r = loss_per_lot(spec, stop, costs) * spec.symbol.volume_min
        rows.append(FeasibilityRow(stop, r, r / equity, r / equity <= limits.max_risk_per_trade_fraction))
    return rows
