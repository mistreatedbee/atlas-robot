"""Live connector check against the real Binance market-data host.

Crypto is OUT OF SCOPE for Atlas (XAU/USD only). The Binance provider is kept as a
verified reference implementation of the provider interface, so this test builds
its own instrument and provider instead of reading the project registry.

Skipped unless ATLAS_LIVE_TESTS=1, because it depends on network access and a
third-party service. Run it before trusting the connector on a new machine.
"""

from __future__ import annotations

import os

import pytest

from atlas.config import ProviderConfig
from atlas.ingest import ingest
from atlas.providers import build_provider

from .conftest import CRYPTO, utc_ms

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("ATLAS_LIVE_TESTS") != "1", reason="set ATLAS_LIVE_TESTS=1 to run"),
]


def test_live_binance_btcusdt_one_day_h1(store):
    provider = build_provider(ProviderConfig("fake", "binance_spot"), None)
    btc = CRYPTO
    start, end = utc_ms(2026, 10, 1), utc_ms(2026, 10, 2)
    result = ingest(store, provider, btc, "H1", start, end)
    assert result.status == "succeeded"
    assert result.run["inserted"] == 24
    assert result.run["rejected"] == 0 and result.run["invalid"] == 0
    assert result.report.unexpected_gaps == []
    again = ingest(store, provider, btc, "H1", start, end)
    assert (again.run["inserted"], again.run["duplicates"], again.run["conflicts"]) == (0, 24, 0)
