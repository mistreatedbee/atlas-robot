"""Execution cost model (docs/13 R1.3, R3.4, R5). All values are PROVISIONAL until measured from Vault tick data."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..models import Candle
from ..sessions import XauCfd


class CostModelError(ValueError):
    pass


@dataclass(frozen=True)
class CostModel:
    base_spread_usd: float  # typical spread, USD/oz
    reopen_spread_usd: float  # spread during the first `reopen_minutes` after a reopen
    reopen_minutes: int
    slippage_usd: float  # per market/stop/time fill, USD/oz (>= 1 tick)
    usd_to_account: float  # constant conversion rate (assumption, docs/13 R5.3)
    status: str  # "provisional_dev" | "verified"
    sources: tuple[str, ...]

    def validate(self, tick_size: float) -> None:
        values = (self.base_spread_usd, self.reopen_spread_usd, self.slippage_usd, self.usd_to_account)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            raise CostModelError("cost values must be finite numbers")
        if self.base_spread_usd < 0 or self.reopen_spread_usd < self.base_spread_usd:
            raise CostModelError("spreads must be >= 0 and the reopen spread >= the base spread")
        if self.slippage_usd < tick_size - 1e-12:
            raise CostModelError(f"slippage {self.slippage_usd} must be at least one tick ({tick_size})")
        if self.usd_to_account <= 0:
            raise CostModelError("usd_to_account must be > 0")
        if self.reopen_minutes < 0:
            raise CostModelError("reopen_minutes must be >= 0")
        if self.status not in ("provisional_dev", "verified"):
            raise CostModelError("status must be provisional_dev or verified")
        if not self.sources:
            raise CostModelError("cost inputs must state their sources")

    def scaled(self, multiplier: float) -> CostModel:
        """Stress copy: spread and slippage x multiplier (FX rate unchanged)."""
        if multiplier < 1:
            raise CostModelError("stress multipliers below 1 would understate costs")
        return replace(self, base_spread_usd=self.base_spread_usd * multiplier,
                       reopen_spread_usd=self.reopen_spread_usd * multiplier,
                       slippage_usd=self.slippage_usd * multiplier,
                       sources=self.sources + (f"stress x{multiplier}",))

    def spread_at(self, candle: Candle, calendar: XauCfd, tick_size: float) -> float:
        model = (self.reopen_spread_usd if calendar.in_reopen_window(candle.open_time, self.reopen_minutes)
                 else self.base_spread_usd)
        reported = candle.spread_points * tick_size if candle.spread_points is not None else 0.0
        return max(model, reported)

    def as_dict(self) -> dict:
        return {"base_spread_usd": self.base_spread_usd, "reopen_spread_usd": self.reopen_spread_usd,
                "reopen_minutes": self.reopen_minutes, "slippage_usd": self.slippage_usd,
                "usd_to_account": self.usd_to_account, "status": self.status, "sources": list(self.sources)}
