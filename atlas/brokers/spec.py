"""Versioned broker contract specification for XAUUSD.

A spec file has a `status`:

* ``unverified``      — only what is known; numeric fields may be missing. Usable for nothing that needs numbers.
* ``provisional_dev`` — generic placeholder values for software tests and simulations. NOT broker facts.
* ``verified``        — every value copied from the broker's MT5 Specification window or written confirmation,
                        with the source recorded.

Consumers declare which statuses they accept; anything needing a missing value fails closed.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any


class SpecError(ValueError):
    pass


class SpecStatus(StrEnum):
    UNVERIFIED = "unverified"
    PROVISIONAL_DEV = "provisional_dev"
    VERIFIED = "verified"


@dataclass(frozen=True)
class SymbolSpec:
    """Values as shown in MT5's Specification window. None = not known."""

    name: str
    contract_size: float | None = None  # ounces per 1.0 lot
    digits: int | None = None
    tick_size: float | None = None  # price increment, USD/oz
    tick_value: float | None = None  # value of one tick move for 1.0 lot, in tick_value_currency
    tick_value_currency: str | None = None
    volume_min: float | None = None  # lots
    volume_max: float | None = None
    volume_step: float | None = None
    stops_level_points: int | None = None  # minimum stop distance in points (1 point = tick_size here)
    freeze_level_points: int | None = None
    spread_typical_points: float | None = None  # indicative only; research uses measured bid/ask
    commission_per_lot_round_turn: float | None = None
    commission_currency: str | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    swap_unit: str | None = None  # e.g. "points" or "money"; as shown by MT5
    swap_triple_day: str | None = None
    margin_initial_per_lot: float | None = None
    margin_currency: str | None = None


@dataclass(frozen=True)
class AccountSpec:
    account_type: str | None = None
    currency: str | None = None  # deposit currency, e.g. "ZAR" or "USD"
    leverage: float | None = None
    margin_call_level_pct: float | None = None
    stop_out_level_pct: float | None = None


@dataclass(frozen=True)
class BrokerSpec:
    status: SpecStatus
    broker: str
    server: str | None
    symbol: SymbolSpec
    account: AccountSpec
    server_time_model: str | None
    sources: tuple[str, ...]
    notes: tuple[str, ...]
    file_sha256: str
    path: Path | None = field(default=None, compare=False)

    # Fields required before a spec can size a position or cost a trade.
    SIZING_FIELDS = ("contract_size", "digits", "tick_size", "volume_min", "volume_max", "volume_step",
                     "stops_level_points", "commission_per_lot_round_turn")

    def missing(self, names: tuple[str, ...] = SIZING_FIELDS) -> list[str]:
        return [n for n in names if getattr(self.symbol, n) is None]

    def require(self, *, allowed: set[SpecStatus], names: tuple[str, ...] = SIZING_FIELDS) -> None:
        """Fail closed unless the spec has an accepted status and every named value."""
        if self.status not in allowed:
            raise SpecError(f"{self.broker} spec status is {self.status.value!r}; this use requires one of "
                            f"{sorted(s.value for s in allowed)}")
        missing = self.missing(names)
        if missing:
            raise SpecError(f"{self.broker} spec is missing required values: {missing}")

    @property
    def label(self) -> str:
        return f"{self.broker} {self.symbol.name} [{self.status.value}] sha256:{self.file_sha256[:12]}"


def _check_consistency(s: SymbolSpec) -> list[str]:
    problems = []
    for name in ("contract_size", "tick_size", "volume_min", "volume_max", "volume_step"):
        v = getattr(s, name)
        if v is not None and (not math.isfinite(v) or v <= 0):
            problems.append(f"{name} must be > 0, got {v}")
    if s.digits is not None and s.tick_size is not None:
        smallest = 10.0 ** -s.digits
        ratio = s.tick_size / smallest
        if ratio < 1 - 1e-9 or abs(ratio - round(ratio)) > 1e-6:
            problems.append(f"tick_size {s.tick_size} is not a whole multiple of 10^-digits ({smallest})")
    if s.volume_min is not None and s.volume_step is not None and s.volume_min + 1e-12 < s.volume_step:
        problems.append(f"volume_min {s.volume_min} < volume_step {s.volume_step}")
    if s.volume_min is not None and s.volume_max is not None and s.volume_max < s.volume_min:
        problems.append(f"volume_max {s.volume_max} < volume_min {s.volume_min}")
    for name in ("stops_level_points", "freeze_level_points", "commission_per_lot_round_turn"):
        v = getattr(s, name)
        if v is not None and v < 0:
            problems.append(f"{name} must be >= 0, got {v}")
    if (s.tick_size is not None and s.tick_value is not None and s.contract_size is not None
            and s.tick_value_currency == "USD"):
        expected = s.tick_size * s.contract_size  # for XAUUSD in a USD account: 1 tick x contract size
        if abs(s.tick_value - expected) > 1e-9 * max(1.0, expected):
            problems.append(f"tick_value {s.tick_value} USD != tick_size x contract_size = {expected}")
    return problems


def _build(cls, raw: dict, where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise SpecError(f"{where}: unknown keys {sorted(unknown)}")
    return cls(**raw)


def load_spec(path: str | Path) -> BrokerSpec:
    path = Path(path)
    data_bytes = path.read_bytes()
    data: dict[str, Any] = tomllib.loads(data_bytes.decode("utf-8"))
    try:
        status = SpecStatus(data["status"])
    except (KeyError, ValueError):
        raise SpecError(f"{path}: 'status' must be one of {[s.value for s in SpecStatus]}") from None
    symbol = _build(SymbolSpec, dict(data.get("symbol", {})), f"{path} [symbol]")
    account = _build(AccountSpec, dict(data.get("account", {})), f"{path} [account]")
    spec = BrokerSpec(
        status=status,
        broker=data.get("broker", ""),
        server=data.get("server"),
        symbol=symbol,
        account=account,
        server_time_model=data.get("server_time", {}).get("model"),
        sources=tuple(data.get("sources", [])),
        notes=tuple(data.get("notes", [])),
        file_sha256=hashlib.sha256(data_bytes).hexdigest(),
        path=path,
    )
    if not spec.broker:
        raise SpecError(f"{path}: 'broker' is required")
    if symbol.name != "XAUUSD" and not symbol.name.startswith("XAUUSD") and symbol.name != "GOLD":
        raise SpecError(f"{path}: symbol {symbol.name!r} does not look like XAU/USD; scope is gold only")
    problems = _check_consistency(symbol)
    if problems:
        raise SpecError(f"{path}: inconsistent values: {problems}")
    if status is SpecStatus.VERIFIED:
        if not spec.sources:
            raise SpecError(f"{path}: a verified spec must list its sources")
        missing = spec.missing() + [n for n in ("server",) if not spec.server]
        if spec.server_time_model is None:
            missing.append("server_time.model")
        if missing:
            raise SpecError(f"{path}: verified spec is missing {missing}")
    return spec
