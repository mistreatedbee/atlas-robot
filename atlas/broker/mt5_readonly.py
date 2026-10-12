"""Read-only MetaTrader 5 access through the official `MetaTrader5` Python package.

Verified facts (MQL5 documentation, 2026-10-12):
* The package connects to a locally installed MT5 terminal; `initialize()` without arguments finds the terminal
  and uses the account and password already saved in it. Atlas therefore never handles credentials.
* The package is not installable on macOS (verified: pip finds no distribution for macOS arm64); it needs a
  Windows machine (PC or VPS) running the MT5 terminal.
* An MT5 login with the INVESTOR password can view the account and prices but cannot trade (metatrader5.com help).

Safeguards in this module:
1. The MT5 module is wrapped in `_ReadOnlyModule`, which exposes ONLY the functions in READ_FUNCTIONS. Any other
   attribute (e.g. trading or order-checking functions) raises immediately, even if code asks for it by name.
2. `connect()` never passes login, password or server.
3. On a REAL account, `connect()` refuses to continue unless the terminal reports trading is NOT allowed (i.e. it
   is logged in with the investor password). On demo/contest accounts this is reported but not required.
4. Everything returned is converted to plain, validated snapshots; nothing here can modify the account.

Times: MT5 returns bar/tick times as epoch seconds that, for most brokers, represent SERVER time, not UTC. They are
converted with the same verified ServerTimeModel used for exported files (assumption until checked on Vault).
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

READ_FUNCTIONS = frozenset({
    "initialize", "shutdown", "version", "last_error", "account_info", "terminal_info", "symbol_info",
    "symbol_info_tick", "symbol_select", "copy_rates_range", "copy_ticks_range", "positions_get", "orders_get",
    "history_deals_get", "history_orders_get",
    # constants needed to call the functions above
    "TIMEFRAME_M1", "TIMEFRAME_M5", "COPY_TICKS_ALL", "ACCOUNT_TRADE_MODE_DEMO", "ACCOUNT_TRADE_MODE_CONTEST",
    "ACCOUNT_TRADE_MODE_REAL",
})


class ReadOnlyViolation(RuntimeError):
    pass


class BrokerReadError(RuntimeError):
    pass


class _ReadOnlyModule:
    def __init__(self, module: Any):
        object.__setattr__(self, "_m", module)

    def __getattr__(self, name: str):
        if name not in READ_FUNCTIONS:
            raise ReadOnlyViolation(f"MT5 function {name!r} is not permitted in the read-only adapter")
        return getattr(object.__getattribute__(self, "_m"), name)

    def __setattr__(self, name, value):
        raise ReadOnlyViolation("the read-only MT5 wrapper is immutable")


def load_mt5_module():
    """Import the official package (Windows only). Kept in one place so tests can inject a fake."""
    if sys.platform != "win32":
        raise BrokerReadError("the MetaTrader5 Python package runs only on Windows (see docs/14)")
    import MetaTrader5  # the ONLY place Atlas imports it; checked by tests/test_config_and_safety.py
    return MetaTrader5


def _get(obj: Any, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    if hasattr(obj, "_asdict"):
        return obj._asdict().get(name)
    return getattr(obj, name, None)


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


@dataclass(frozen=True)
class AccountSnapshot:
    login: int
    server: str
    company: str
    currency: str
    trade_mode: str  # "demo" | "contest" | "real" | "unknown"
    trade_allowed: bool
    leverage: float
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_so_call: float
    margin_so_so: float
    taken_at_ms: int


@dataclass(frozen=True)
class SymbolSnapshot:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    swap_long: float
    swap_short: float
    swap_rollover3days: int
    currency_profit: str
    currency_margin: str
    trade_mode: int
    filling_mode: int
    spread_points: int
    spread_float: bool
    taken_at_ms: int


@dataclass(frozen=True)
class TickSnapshot:
    symbol: str
    time_server_s: int  # as returned by MT5 (server time, see module docstring)
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class PositionSnapshot:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str


class Mt5ReadOnly:
    def __init__(self, module: Any | None = None, *, clock=lambda: int(datetime.now(timezone.utc).timestamp() * 1000)):
        self.mt5 = _ReadOnlyModule(module if module is not None else load_mt5_module())
        self._clock = clock
        self.account: AccountSnapshot | None = None

    # ---- connection ---------------------------------------------------------------------------------

    def connect(self) -> AccountSnapshot:
        """Attach to the running terminal with the credentials saved IN THE TERMINAL; none pass through Atlas."""
        if not self.mt5.initialize():
            raise BrokerReadError(f"MT5 initialize() failed: {self.mt5.last_error()}")
        acc = self.account_snapshot()
        if acc.trade_mode == "real" and acc.trade_allowed:
            self.mt5.shutdown()
            raise ReadOnlyViolation(
                "connected REAL account has trading enabled. For the read-only stage, log the MT5 terminal in with "
                "the INVESTOR (read-only) password so the broker itself blocks trading, then retry.")
        if acc.trade_mode == "unknown":
            self.mt5.shutdown()
            raise BrokerReadError("account trade mode could not be determined; refusing to continue")
        self.account = acc
        return acc

    def close(self) -> None:
        self.mt5.shutdown()

    # ---- snapshots ----------------------------------------------------------------------------------

    def account_snapshot(self) -> AccountSnapshot:
        a = self.mt5.account_info()
        if a is None:
            raise BrokerReadError(f"account_info() returned nothing: {self.mt5.last_error()}")
        modes = {self.mt5.ACCOUNT_TRADE_MODE_DEMO: "demo", self.mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
                 self.mt5.ACCOUNT_TRADE_MODE_REAL: "real"}
        snap = AccountSnapshot(
            login=int(_get(a, "login")), server=str(_get(a, "server")), company=str(_get(a, "company") or ""),
            currency=str(_get(a, "currency")), trade_mode=modes.get(_get(a, "trade_mode"), "unknown"),
            trade_allowed=bool(_get(a, "trade_allowed")), leverage=float(_get(a, "leverage")),
            balance=float(_get(a, "balance")), equity=float(_get(a, "equity")), margin=float(_get(a, "margin")),
            margin_free=float(_get(a, "margin_free")), margin_so_call=float(_get(a, "margin_so_call")),
            margin_so_so=float(_get(a, "margin_so_so")), taken_at_ms=self._clock())
        for f in ("leverage", "balance", "equity", "margin", "margin_free"):
            if not _finite(getattr(snap, f)):
                raise BrokerReadError(f"account field {f} is not a finite number")
        return snap

    def symbol_snapshot(self, symbol: str) -> SymbolSnapshot:
        self.mt5.symbol_select(symbol, True)  # make sure the terminal streams it (read-only operation)
        s = self.mt5.symbol_info(symbol)
        if s is None:
            raise BrokerReadError(f"symbol {symbol!r} not found on this server: {self.mt5.last_error()}")
        snap = SymbolSnapshot(
            name=str(_get(s, "name")), digits=int(_get(s, "digits")), point=float(_get(s, "point")),
            tick_size=float(_get(s, "trade_tick_size")), tick_value=float(_get(s, "trade_tick_value")),
            contract_size=float(_get(s, "trade_contract_size")), volume_min=float(_get(s, "volume_min")),
            volume_max=float(_get(s, "volume_max")), volume_step=float(_get(s, "volume_step")),
            stops_level_points=int(_get(s, "trade_stops_level")),
            freeze_level_points=int(_get(s, "trade_freeze_level")), swap_long=float(_get(s, "swap_long")),
            swap_short=float(_get(s, "swap_short")), swap_rollover3days=int(_get(s, "swap_rollover3days")),
            currency_profit=str(_get(s, "currency_profit")), currency_margin=str(_get(s, "currency_margin")),
            trade_mode=int(_get(s, "trade_mode")), filling_mode=int(_get(s, "filling_mode")),
            spread_points=int(_get(s, "spread")), spread_float=bool(_get(s, "spread_float")),
            taken_at_ms=self._clock())
        for f in ("point", "tick_size", "contract_size", "volume_min", "volume_step"):
            if not (_finite(getattr(snap, f)) and getattr(snap, f) > 0):
                raise BrokerReadError(f"symbol field {f} is missing or not positive")
        return snap

    def tick(self, symbol: str) -> TickSnapshot:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise BrokerReadError(f"no tick for {symbol}: {self.mt5.last_error()}")
        bid, ask = float(_get(t, "bid")), float(_get(t, "ask"))
        if not (_finite(bid) and _finite(ask) and 0 < bid <= ask):
            raise BrokerReadError(f"invalid quote bid={bid} ask={ask}")
        return TickSnapshot(symbol, int(_get(t, "time")), bid, ask)

    def positions(self, symbol: str) -> list[PositionSnapshot]:
        rows = self.mt5.positions_get(symbol=symbol)
        if rows is None:
            raise BrokerReadError(f"positions_get failed: {self.mt5.last_error()}")
        return [PositionSnapshot(int(_get(p, "ticket")), str(_get(p, "symbol")),
                                 "buy" if int(_get(p, "type")) == 0 else "sell", float(_get(p, "volume")),
                                 float(_get(p, "price_open")), float(_get(p, "sl")), float(_get(p, "tp")),
                                 int(_get(p, "magic")), str(_get(p, "comment") or "")) for p in rows]

    def deals(self, start_utc: datetime, end_utc: datetime) -> list:
        rows = self.mt5.history_deals_get(start_utc, end_utc)
        if rows is None:
            raise BrokerReadError(f"history_deals_get failed: {self.mt5.last_error()}")
        return list(rows)

    def rates_range(self, symbol: str, timeframe: str, start_utc: datetime, end_utc: datetime) -> list[dict]:
        tf = {"M1": self.mt5.TIMEFRAME_M1, "M5": self.mt5.TIMEFRAME_M5}[timeframe]
        rows = self.mt5.copy_rates_range(symbol, tf, start_utc, end_utc)
        if rows is None:
            raise BrokerReadError(f"copy_rates_range failed: {self.mt5.last_error()}")
        return [{k: _get(r, k) for k in ("time", "open", "high", "low", "close", "tick_volume", "spread")}
                for r in rows]


@dataclass(frozen=True)
class CommissionEstimate:
    per_lot_round_turn: float | None  # account currency per 1.0 lot, entry + exit; None if no evidence
    currency: str
    deals_used: int
    note: str


def commission_from_deals(deals: list, symbol: str, currency: str) -> CommissionEstimate:
    """Infer commission per lot round turn from executed deals (history_deals_get): sum of commission over the
    entry and exit deals of the symbol divided by the volume of entries. MT5 reports commission as a negative
    amount in the account currency. No deals -> None (never assumed)."""
    rows = [d for d in deals if str(_get(d, "symbol")) == symbol]
    entries = [d for d in rows if int(_get(d, "entry")) == 0]  # DEAL_ENTRY_IN
    entry_volume = sum(float(_get(d, "volume")) for d in entries)
    if not rows or entry_volume <= 0:
        return CommissionEstimate(None, currency, 0, "no executed deals for this symbol yet: do one demo trade")
    total = -sum(float(_get(d, "commission") or 0.0) for d in rows)
    if total < 0:
        return CommissionEstimate(None, currency, len(rows), "positive commission reported: needs manual review")
    return CommissionEstimate(round(total / entry_volume, 6), currency, len(rows),
                              "from executed deals (entry+exit) / entry volume")


def spec_toml_from_snapshots(sym: SymbolSnapshot, acc: AccountSnapshot, *, broker: str, server_time_model: str,
                             source_note: str) -> str:
    """Render a broker spec file (atlas/brokers/spec.py format) from live terminal data. Status is 'verified'
    because every value comes from the broker's own terminal; the server-time model must already have passed
    the data check (docs/10) and is passed in explicitly."""
    taken = datetime.fromtimestamp(sym.taken_at_ms / 1000, timezone.utc).isoformat(timespec="seconds")
    return f'''# Generated from the live MT5 terminal (read-only) at {taken}. Do not edit values by hand.
status = "verified"
broker = "{broker}"
server = "{acc.server}"
sources = ["MT5 symbol_info/account_info on {acc.server} at {taken} ({source_note})"]
notes = ["account trade mode: {acc.trade_mode}; trading allowed in this session: {acc.trade_allowed}"]

[symbol]
name = "{sym.name}"
contract_size = {sym.contract_size!r}
digits = {sym.digits}
tick_size = {sym.tick_size!r}
tick_value = {sym.tick_value!r}
tick_value_currency = "{acc.currency}"
volume_min = {sym.volume_min!r}
volume_max = {sym.volume_max!r}
volume_step = {sym.volume_step!r}
stops_level_points = {sym.stops_level_points}
freeze_level_points = {sym.freeze_level_points}
spread_typical_points = {sym.spread_points}
swap_long = {sym.swap_long!r}
swap_short = {sym.swap_short!r}
swap_triple_day = "{sym.swap_rollover3days}"
margin_currency = "{sym.currency_margin}"
# commission is NOT available from symbol_info: fill it from deal history (history_deals_get) or support's reply

[account]
currency = "{acc.currency}"
leverage = {acc.leverage!r}
margin_call_level_pct = {acc.margin_so_call!r}
stop_out_level_pct = {acc.margin_so_so!r}

[server_time]
model = "{server_time_model}"
'''
