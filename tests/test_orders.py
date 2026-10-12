"""Order preparation tests. PROVISIONAL dev spec and test-only quotes; nothing is ever sent."""

from __future__ import annotations

from dataclasses import replace

import pytest

from atlas.broker.mt5_readonly import TickSnapshot
from atlas.broker.orders import Target, client_order_id, prepare_order
from atlas.brokers.spec import load_spec
from atlas.risk.gate import Check, Mode, RiskDecision

from .conftest import ROOT

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
UNVERIFIED = load_spec(ROOT / "config/brokers/vault_markets_xauusd.toml")
NOW = 1_800_000_000_000
TICK = TickSnapshot("XAUUSD", 0, 2400.10, 2400.40)
APPROVED = RiskDecision(True, 0.05, 100.0, 0.01, (Check("x", True, ""),), Mode.PAPER, "s", ("t",))


def prep(**kw):
    args = dict(target=Target.PAPER, spec=SPEC, decision=APPROVED, side="buy", stop_distance=3.0, tp_distance=4.5,
                tick=TICK, quote_time_ms=NOW - 200, now_ms=NOW, strategy_id="S1", decision_time_ms=NOW - 1000)
    args.update(kw)
    return prepare_order(**args)


def rules(p):
    return {c.rule for c in p.failed}


def test_buy_prepared_at_ask_with_rounded_levels():
    p = prep()
    o = p.order
    assert p.ok and (o.side, o.expected_price, o.volume) == ("buy", 2400.40, 0.05)
    assert (o.sl, o.tp) == (2397.40, 2404.90)
    assert o.comment == f"atlas:{o.client_order_id}" and len(o.comment) <= 31
    assert o.valid_until_ms == NOW + 5000 and o.target is Target.PAPER


def test_sell_prepared_at_bid():
    o = prep(side="sell", stop_distance=3.005, tp_distance=4.505).order
    assert o.expected_price == 2400.10
    assert (o.sl, o.tp) == (2403.11, 2395.60)  # SL rounded away (up), TP toward entry (up)


def test_client_order_id_is_deterministic_per_decision():
    a, b = prep().order, prep().order
    assert a.client_order_id == b.client_order_id == client_order_id("S1", "XAUUSD", "buy", NOW - 1000)
    assert prep(decision_time_ms=NOW - 2000).order.client_order_id != a.client_order_id


@pytest.mark.parametrize("kw, rule", [
    ({"decision": replace(APPROVED, approved=False)}, "risk_decision_approved"),
    ({"quote_time_ms": NOW - 5_000}, "quote_fresh"),
    ({"tick": TickSnapshot("XAUUSD", 0, 2400.5, 2400.1)}, "quote_valid"),
    ({"side": "hold"}, "side"),
    ({"stop_distance": 0.0}, "distances"),
    ({"decision": replace(APPROVED, lots=0.015)}, "volume_step"),
    ({"decision": replace(APPROVED, lots=60.0)}, "volume_range"),
])
def test_invalid_inputs_rejected(kw, rule):
    p = prep(**kw)
    assert not p.ok and rule in rules(p)


def test_stops_level_enforced_from_current_prices(tmp_path):
    text = (ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml").read_text(encoding="utf-8").replace(
        "stops_level_points = 0", "stops_level_points = 350")  # 3.50 USD minimum distance
    (tmp_path / "s.toml").write_text(text, encoding="utf-8")
    spec = load_spec(tmp_path / "s.toml")
    p = prepare_order(target=Target.PAPER, spec=spec, decision=APPROVED, side="buy", stop_distance=3.0,
                      tp_distance=4.5, tick=TICK, quote_time_ms=NOW, now_ms=NOW, strategy_id="S1",
                      decision_time_ms=NOW)
    # buy SL 2397.40 is only 2.70 below the bid (2400.10): closer than 3.50 -> rejected, never adjusted silently
    assert not p.ok and "sl_side_and_distance" in rules(p)


def test_unverified_vault_spec_cannot_prepare_orders():
    p = prep(spec=UNVERIFIED)
    assert not p.ok and "broker_spec" in rules(p)


def test_symbol_mismatch_rejected():
    p = prep(tick=TickSnapshot("XAUUSD.m", 0, 2400.1, 2400.4))
    assert not p.ok and "symbol" in rules(p)


def test_there_is_no_live_target():
    assert [t.value for t in Target] == ["paper"]
    p = prep(target="live")
    assert not p.ok and "target" in rules(p)
