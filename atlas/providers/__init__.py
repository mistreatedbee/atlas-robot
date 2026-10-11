"""Provider factory. Providers are selected by the `kind` field in atlas.toml."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..config import ConfigError, ProviderConfig
from ..http import ReadOnlyHttpClient
from ..models import DataLabel, PriceBasis, VolumeKind
from .base import CandleProvider
from .binance_spot import DEFAULT_BASE_URL, BinanceSpotProvider
from .csv_file import CsvFileProvider

__all__ = ["CandleProvider", "BinanceSpotProvider", "CsvFileProvider", "build_provider"]


def build_provider(cfg: ProviderConfig, root: Path) -> CandleProvider:
    opts = cfg.options
    if cfg.kind == "binance_spot":
        base_url = opts.get("base_url", DEFAULT_BASE_URL)
        http = ReadOnlyHttpClient(
            allowed_hosts={urlsplit(base_url).hostname},
            min_interval_s=float(opts.get("min_request_interval_s", 0.25)),
            max_retries=int(opts.get("max_retries", 5)),
        )
        return BinanceSpotProvider(http, name=cfg.name, base_url=base_url,
                                   weight_safety_fraction=float(opts.get("weight_safety_fraction", 0.5)))
    if cfg.kind == "csv_file":
        try:
            return CsvFileProvider(
                name=cfg.name,
                venue=opts["venue"],
                path_template=root / opts["path"],
                data_label=DataLabel(opts["data_label"]),
                price_basis=PriceBasis(opts["price_basis"]),
                volume_kind=VolumeKind(opts.get("volume_kind", "none")),
                source_timezone=opts.get("source_timezone", "UTC"),
            )
        except KeyError as exc:
            raise ConfigError(f"provider {cfg.name}: missing option {exc}") from None
    raise ConfigError(f"provider {cfg.name}: unknown kind {cfg.kind!r}")
