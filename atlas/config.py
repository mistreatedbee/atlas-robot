"""Configuration loading: settings, provider definitions and the instrument registry.

Instruments are configuration, not code. Adding one means adding a TOML block and
mapping it to the symbol each provider uses; no agent or module is edited.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sessions import CALENDARS

ASSET_CLASSES = {"metal_spot", "fx_spot", "crypto_spot"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    asset_class: str
    base_currency: str
    quote_currency: str
    session_calendar: str
    price_increment: float | None
    symbols: dict[str, str]  # provider name -> provider's own symbol
    enabled: bool = True
    rationale: str = ""
    contract: dict[str, Any] = field(default_factory=dict)  # broker contract spec; unconfirmed until verified

    def symbol_for(self, provider: str) -> str:
        try:
            return self.symbols[provider]
        except KeyError:
            raise ConfigError(
                f"instrument {self.instrument_id} has no symbol mapping for provider {provider!r}; "
                f"mapped providers: {sorted(self.symbols)}"
            ) from None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id, "asset_class": self.asset_class,
            "base_currency": self.base_currency, "quote_currency": self.quote_currency,
            "session_calendar": self.session_calendar, "price_increment": self.price_increment,
            "symbols": dict(self.symbols), "enabled": self.enabled, "rationale": self.rationale,
            "contract": dict(self.contract),
        }


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    log_path: Path
    instruments: dict[str, Instrument]
    providers: dict[str, ProviderConfig]
    authorised_instruments: frozenset[str] = frozenset()

    def instrument(self, instrument_id: str) -> Instrument:
        """Look up an instrument that is both enabled and inside the authorised project scope."""
        try:
            inst = self.instruments[instrument_id]
        except KeyError:
            raise ConfigError(f"unknown instrument {instrument_id!r}; known: {sorted(self.instruments)}") from None
        if not inst.enabled:
            raise ConfigError(f"instrument {instrument_id} is disabled in the registry")
        return inst

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise ConfigError(f"unknown provider {name!r}; known: {sorted(self.providers)}") from None


def _require(table: dict, key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return table[key]


def parse_instruments(data: dict, provider_names: set[str]) -> dict[str, Instrument]:
    instruments: dict[str, Instrument] = {}
    for i, raw in enumerate(data.get("instrument", [])):
        where = f"instrument[{i}]"
        iid = _require(raw, "id", where)
        where = f"instrument {iid}"
        if iid in instruments:
            raise ConfigError(f"duplicate instrument id {iid!r}")
        asset_class = _require(raw, "asset_class", where)
        if asset_class not in ASSET_CLASSES:
            raise ConfigError(f"{where}: asset_class {asset_class!r} not in {sorted(ASSET_CLASSES)}")
        calendar = _require(raw, "session_calendar", where)
        if calendar not in CALENDARS:
            raise ConfigError(f"{where}: session_calendar {calendar!r} not in {sorted(CALENDARS)}")
        symbols = dict(raw.get("symbols", {}))
        unknown = set(symbols) - provider_names
        if unknown:
            raise ConfigError(f"{where}: symbols reference undefined providers {sorted(unknown)}")
        instruments[iid] = Instrument(
            instrument_id=iid,
            asset_class=asset_class,
            base_currency=_require(raw, "base", where),
            quote_currency=_require(raw, "quote", where),
            session_calendar=calendar,
            price_increment=raw.get("price_increment"),
            symbols=symbols,
            enabled=bool(raw.get("enabled", True)),
            rationale=raw.get("rationale", ""),
            contract=dict(raw.get("contract", {})),
        )
    return instruments


def check_scope(instruments: dict[str, Instrument], authorised: frozenset[str]) -> None:
    """The project scope is a hard boundary: an enabled instrument outside it is a configuration error."""
    if not authorised:
        raise ConfigError("[scope] authorised_instruments must list at least one instrument")
    outside = sorted(i.instrument_id for i in instruments.values() if i.enabled and i.instrument_id not in authorised)
    if outside:
        raise ConfigError(f"enabled instruments outside the authorised scope {sorted(authorised)}: {outside}")


def load_settings(config_path: str | Path) -> Settings:
    config_path = Path(config_path).resolve()
    root = config_path.parent
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    providers = {
        name: ProviderConfig(name=name, kind=_require(raw, "kind", f"provider {name}"),
                             options={k: v for k, v in raw.items() if k != "kind"})
        for name, raw in data.get("providers", {}).items()
    }
    instruments_file = root / data.get("instruments_file", "config/instruments.toml")
    with instruments_file.open("rb") as fh:
        instruments = parse_instruments(tomllib.load(fh), set(providers))

    authorised = frozenset(data.get("scope", {}).get("authorised_instruments", []))
    check_scope(instruments, authorised)
    storage = data.get("storage", {})
    return Settings(
        root=root,
        database_path=root / storage.get("database_path", "data/atlas.db"),
        log_path=root / storage.get("log_path", "logs/atlas.jsonl"),
        instruments=instruments,
        providers=providers,
        authorised_instruments=authorised,
    )
