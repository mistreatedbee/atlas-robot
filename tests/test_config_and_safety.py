from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.config import ConfigError, check_scope, load_settings, parse_instruments
from atlas.http import ReadOnlyHttpClient
from atlas.providers import build_provider

from .conftest import ROOT


def test_repository_config_loads():
    s = load_settings(ROOT / "atlas.toml")
    for name in s.providers:
        build_provider(s.provider(name), s.root)  # every configured provider can be constructed


def test_scope_is_xauusd_only():
    s = load_settings(ROOT / "atlas.toml")
    assert s.authorised_instruments == {"XAUUSD"}
    assert [i.instrument_id for i in s.instruments.values() if i.enabled] == ["XAUUSD"]
    gold = s.instrument("XAUUSD")
    assert (gold.asset_class, gold.base_currency, gold.quote_currency) == ("metal_spot", "XAU", "USD")
    assert gold.session_calendar == "xau_cfd"
    assert "binance_spot" not in s.providers  # crypto is out of scope


def test_enabled_instrument_outside_scope_is_a_config_error():
    inst = parse_instruments({"instrument": [
        {"id": "XAUUSD", "asset_class": "metal_spot", "base": "XAU", "quote": "USD", "session_calendar": "xau_cfd"},
        {"id": "EURUSD", "asset_class": "fx_spot", "base": "EUR", "quote": "USD", "session_calendar": "fx_24x5"},
    ]}, set())
    with pytest.raises(ConfigError, match="outside the authorised scope"):
        check_scope(inst, frozenset({"XAUUSD"}))
    with pytest.raises(ConfigError, match="at least one"):
        check_scope(inst, frozenset())


def test_disabled_instrument_cannot_be_selected(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.toml").write_text(
        '[[instrument]]\nid = "XAUUSD"\nasset_class = "metal_spot"\nbase = "XAU"\nquote = "USD"\n'
        'session_calendar = "xau_cfd"\nenabled = false\n')
    (tmp_path / "atlas.toml").write_text('[scope]\nauthorised_instruments = ["XAUUSD"]\n')
    with pytest.raises(ConfigError, match="disabled"):
        load_settings(tmp_path / "atlas.toml").instrument("XAUUSD")


@pytest.mark.parametrize("raw, message", [
    ({"instrument": [{"id": "A", "asset_class": "fx_spot", "base": "E", "quote": "U", "session_calendar": "fx_24x5"},
                     {"id": "A", "asset_class": "fx_spot", "base": "E", "quote": "U", "session_calendar": "fx_24x5"}]},
     "duplicate"),
    ({"instrument": [{"id": "A", "asset_class": "equity", "base": "E", "quote": "U", "session_calendar": "fx_24x5"}]},
     "asset_class"),
    ({"instrument": [{"id": "A", "asset_class": "fx_spot", "base": "E", "quote": "U", "session_calendar": "lunar"}]},
     "session_calendar"),
    ({"instrument": [{"id": "A", "asset_class": "fx_spot", "base": "E", "quote": "U", "session_calendar": "fx_24x5",
                      "symbols": {"nonexistent": "X"}}]}, "undefined providers"),
    ({"instrument": [{"id": "A", "asset_class": "fx_spot", "quote": "U", "session_calendar": "fx_24x5"}]}, "base"),
])
def test_invalid_registry_is_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_instruments(raw, {"sample_synthetic"})


# ---- Phase 1 safety boundary: no trading capability exists ------------------------------------

SOURCE = [p for p in (ROOT / "atlas").rglob("*.py")]
FORBIDDEN = [
    r"/api/v3/order", r"/fapi/v\d/order", r"newOrder", r"X-MBX-APIKEY", r"hmac", r"signature",
    r"api_secret", r"secret_key", r"place_order", r"submit_order",
    # HTTP write verbs can only be sent as string literals; urllib sends POST implicitly when data= is passed.
    r"[\"']POST[\"']", r"[\"']DELETE[\"']", r"[\"']PUT[\"']", r"[\"']PATCH[\"']",
    r"Request\([^)]*data\s*=", r"urlopen\([^)]*data\s*=",
    r"order_send", r"order_check", r"MetaTrader5", r"\bmt5\.initialize", r"TRADE_ACTION_",
]


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_no_order_or_credential_code_in_package(pattern):
    hits = [f"{p.relative_to(ROOT)}" for p in SOURCE if re.search(pattern, p.read_text(), re.IGNORECASE)]
    assert hits == [], f"pattern {pattern!r} found in {hits}"


def test_http_client_has_no_write_methods():
    public = {n for n in dir(ReadOnlyHttpClient) if not n.startswith("_")}
    assert public == {"get_json", "RETRYABLE_STATUS"}


def test_dormant_binance_reference_provider_uses_market_data_only_host():
    from atlas.providers.binance_spot import DEFAULT_BASE_URL
    assert DEFAULT_BASE_URL == "https://data-api.binance.vision"


def test_no_credentials_in_config_files():
    for path in [ROOT / "atlas.toml", *Path(ROOT / "config").glob("*.toml")]:
        assert not re.search(r"(api[_-]?key|secret|token|password)\s*=", path.read_text(), re.IGNORECASE), path
