from __future__ import annotations

import pytest

from atlas.brokers.spec import SpecError, SpecStatus, load_spec

from .conftest import ROOT

VALID = """
status = "{status}"
broker = "Test Broker"
server = "Test-Demo"
sources = ["unit test"]
[symbol]
name = "XAUUSD"
contract_size = 100.0
digits = 2
tick_size = 0.01
tick_value = 1.0
tick_value_currency = "USD"
volume_min = 0.01
volume_max = 50.0
volume_step = 0.01
stops_level_points = 0
commission_per_lot_round_turn = 0.0
[account]
currency = "ZAR"
leverage = 500.0
stop_out_level_pct = 50.0
[server_time]
model = "ny_close_midnight"
"""


def write(tmp_path, text, name="spec.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_repository_vault_spec_is_unverified_and_holds_no_numbers():
    spec = load_spec(ROOT / "config/brokers/vault_markets_xauusd.toml")
    assert spec.status is SpecStatus.UNVERIFIED
    assert spec.missing() == list(spec.SIZING_FIELDS)  # nothing invented
    with pytest.raises(SpecError, match="status"):
        spec.require(allowed={SpecStatus.VERIFIED, SpecStatus.PROVISIONAL_DEV})


def test_repository_provisional_spec_is_labelled_and_complete():
    spec = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
    assert spec.status is SpecStatus.PROVISIONAL_DEV
    assert "PROVISIONAL" in spec.broker
    spec.require(allowed={SpecStatus.PROVISIONAL_DEV})
    with pytest.raises(SpecError):
        spec.require(allowed={SpecStatus.VERIFIED})  # can never stand in for a verified spec


def test_verified_spec_loads_with_hash(tmp_path):
    spec = load_spec(write(tmp_path, VALID.format(status="verified")))
    assert spec.status is SpecStatus.VERIFIED and len(spec.file_sha256) == 64
    spec.require(allowed={SpecStatus.VERIFIED})


@pytest.mark.parametrize("old, new, message", [
    ("tick_size = 0.01", "tick_size = 0.015", "whole multiple"),
    ("tick_size = 0.01", "tick_size = 0.001", "whole multiple"),
    ("volume_min = 0.01", "volume_min = 0.001", "volume_min"),
    ("volume_max = 50.0", "volume_max = 0.001", "volume_max"),
    ("contract_size = 100.0", "contract_size = 0.0", "contract_size"),
    ("tick_value = 1.0", "tick_value = 0.1", "tick_value"),
    ("commission_per_lot_round_turn = 0.0", "commission_per_lot_round_turn = -1.0", "commission"),
    ('name = "XAUUSD"', 'name = "EURUSD"', "gold only"),
    ('status = "verified"', 'status = "maybe"', "status"),
    ("digits = 2", "digits = 2\nfoo = 1", "unknown keys"),
])
def test_inconsistent_specs_are_rejected(tmp_path, old, new, message):
    text = VALID.format(status="verified").replace(old, new)
    with pytest.raises(SpecError, match=message):
        load_spec(write(tmp_path, text))


@pytest.mark.parametrize("remove", ["contract_size = 100.0", 'server = "Test-Demo"', 'model = "ny_close_midnight"',
                                    'sources = ["unit test"]', 'tick_value = 1.0', 'currency = "ZAR"',
                                    "leverage = 500.0", "stop_out_level_pct = 50.0"])
def test_verified_spec_must_be_complete(tmp_path, remove):
    text = VALID.format(status="verified").replace(remove, "")
    with pytest.raises(SpecError):
        load_spec(write(tmp_path, text))


def test_unverified_spec_may_be_partial_but_fails_closed_when_used(tmp_path):
    spec = load_spec(write(tmp_path, VALID.format(status="unverified").replace("volume_min = 0.01", "")))
    assert spec.missing() == ["volume_min"]
    with pytest.raises(SpecError, match="missing"):
        spec.require(allowed={SpecStatus.UNVERIFIED})
