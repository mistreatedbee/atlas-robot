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
    # HTTP write verbs as words (catches requests.post, httpx.put, session.delete, HTTPMethod.POST, "PATCH"...),
    # plus urllib's implicit POST when a body is passed. Only SQL trigger syntax is exempted (see _scan_text).
    r"\bPOST\b", r"\bDELETE\b", r"\bPUT\b", r"\bPATCH\b",
    r"\bhttp\.client\b", r"\bHTTPS?Connection\b",
    r"order_send", r"order_check", r"MetaTrader5", r"\bmt5\.initialize", r"TRADE_ACTION_",
]


def _scan_text(path: Path) -> str:
    """Source text with ONLY SQL trigger clauses removed (e.g. 'BEFORE DELETE ON experiments')."""
    return re.sub(r"\bBEFORE\s+(DELETE|UPDATE|INSERT)\b", "", path.read_text(), flags=re.IGNORECASE)


import ast  # noqa: E402

NETWORK_MODULES = {"socket", "http.client", "requests", "httpx", "aiohttp", "urllib3", "MetaTrader5",
                   "ftplib", "smtplib", "websocket", "websockets"}


def ast_violations(source: str) -> list[str]:
    """Syntax-tree check for anything that could SEND data: Request/urlopen with a body (positional or data=,
    or unpackable */** arguments), custom openers, and imports of raw network / trading libraries."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            out += [f"import {n}" for n in names if n in NETWORK_MODULES or n.split(".")[0] in NETWORK_MODULES]
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in ("build_opener", "OpenerDirector"):
                out.append(name)
            if name in ("Request", "urlopen"):
                body = (len(node.args) > 1 or any(k.arg in (None, "data") for k in node.keywords)
                        or any(isinstance(a, ast.Starred) for a in node.args))
                if body:
                    out.append(f"{name} with a body")
    return out


@pytest.mark.parametrize("snippet", [
    "requests.post(url)", "httpx.put(u)", "session.delete(x)", "HTTPMethod.POST", 'method="PATCH"',
    "conn = http.client.HTTPSConnection(h)",
])
def test_scan_catches_write_verbs(snippet):
    assert any(re.search(p, snippet, re.IGNORECASE) for p in FORBIDDEN), snippet


@pytest.mark.parametrize("snippet", [
    "urllib.request.Request(url, data=b'x')", "urlopen(url, payload)", "urlopen(build(u), data=b'')",
    "urllib.request.Request(url, payload)", "urlopen(Request(url), body)", "Request(\n    url,\n    data=b'x')",
    "Request(url, json.dumps(p).encode())", "Request(build(u), body)", "Request(**kw)", "urlopen(Request(make(u)), body)",
    "opener = urllib.request.build_opener()", "import socket", "from http.client import HTTPSConnection",
    "import requests", "import MetaTrader5 as mt5",
])
def test_ast_scan_catches_request_bodies_and_network_libraries(snippet):
    assert ast_violations(snippet), snippet


@pytest.mark.parametrize("snippet", [
    "with urlopen(req) as r:\n    data = r.read()",          # reading a response is fine
    "req = Request(url, method='GET', headers=h)",
    "data = json.loads(body)",
])
def test_ast_scan_has_no_false_positives_on_reads(snippet):
    assert ast_violations(snippet) == []


def test_package_has_no_ast_violations():
    hits = {str(p.relative_to(ROOT)): v for p in SOURCE if (v := ast_violations(p.read_text()))}
    assert hits == {}


def test_scan_exempts_only_sql_trigger_clauses(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("CREATE TRIGGER t BEFORE DELETE ON experiments\nrequests.delete(u)\n")
    text = _scan_text(f)
    assert "BEFORE DELETE" not in text and re.search(r"\bDELETE\b", text, re.IGNORECASE)


def test_http_transport_sends_only_bodiless_get(monkeypatch):
    import urllib.request

    from atlas import http as http_mod
    seen = []

    class FakeResp:
        status = 200
        headers = {}

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None, context=None):
        seen.append(request)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    http_mod.urllib_transport("https://data-api.binance.vision/x", 5)
    (req,) = seen
    assert req.get_method() == "GET" and req.data is None


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_no_order_or_credential_code_in_package(pattern):
    hits = [f"{p.relative_to(ROOT)}" for p in SOURCE if re.search(pattern, _scan_text(p), re.IGNORECASE)]
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
