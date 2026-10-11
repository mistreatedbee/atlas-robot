"""Risk gate tests. Uses the PROVISIONAL dev spec and test-only FX/cost values; none are Vault Markets facts."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from atlas.brokers.spec import load_spec
from atlas.risk.gate import (
    AccountState, CostInputs, KillSwitch, Mode, RiskConfigError, RiskLimits, TradeIntent, evaluate, feasibility,
    loss_per_lot,
)

from .conftest import ROOT

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
UNVERIFIED = load_spec(ROOT / "config/brokers/vault_markets_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
NOW = 1_800_000_000_000
COSTS = CostInputs(spread_usd=0.30, slippage_usd=0.10, usd_to_account=16.6, sources=("test-only values",))


def account(equity=500.0, **kw):
    base = AccountState(equity=equity, free_margin=equity, day_start_equity=equity, peak_equity=equity,
                        open_risk=0.0, trades_today=0, verified_at_ms=NOW - 1_000)
    return replace(base, **kw)


def buy(stop_distance, entry=2400.0, **kw):
    return replace(TradeIntent("buy", entry, entry - stop_distance, NOW - 100), **kw)


@pytest.fixture
def clear_switch(tmp_path):
    return KillSwitch(tmp_path / "KILL_SWITCH")


def run(intent, acct, spec=SPEC, costs=COSTS, limits=LIMITS, switch=None, mode=Mode.BACKTEST):
    return evaluate(intent, spec, acct, limits, costs, now_ms=NOW, mode=mode, kill_switch=switch)


def failed_rules(decision):
    return {c.rule for c in decision.failed}


def test_r500_account_cannot_take_min_lot_gold_trade_at_1pct(clear_switch):
    # 0.01 lot x 100 oz x (US$1 stop + 0.40 costs) x 16.6 = R23.24 = 4.6% of R500 > 1% limit -> rejected
    d = run(buy(1.0), account(500), switch=clear_switch)
    assert not d.approved and failed_rules(d) == {"minimum_volume_within_risk_limit"}
    detail = next(c.detail for c in d.checks if c.rule == "minimum_volume_within_risk_limit")
    assert "23.24" in detail and "4.65%" in detail


def test_larger_account_is_sized_down_to_the_limit(clear_switch):
    d = run(buy(3.0), account(50_000), switch=clear_switch)
    assert d.approved
    per_lot = loss_per_lot(SPEC, 3.0, COSTS)  # (3.40 x 100) x 16.6 = 5644 per lot
    assert d.lots == pytest.approx(math.floor(500 / per_lot / 0.01) * 0.01)
    assert d.risk_fraction <= LIMITS.max_risk_per_trade_fraction + 1e-12


def test_size_never_increases_after_losses(clear_switch):
    healthy = run(buy(3.0), account(50_000), switch=clear_switch)
    after_loss = run(buy(3.0), account(48_800, day_start_equity=50_000, peak_equity=50_000), switch=clear_switch)
    assert after_loss.approved and after_loss.lots <= healthy.lots


def test_sell_side_and_wrong_side_stop(clear_switch):
    sell = TradeIntent("sell", 2400.0, 2403.0, NOW - 100)
    assert run(sell, account(50_000), switch=clear_switch).approved
    wrong = TradeIntent("sell", 2400.0, 2397.0, NOW - 100)
    assert "stop_on_correct_side" in failed_rules(run(wrong, account(50_000), switch=clear_switch))


@pytest.mark.parametrize("acct_kw, rule", [
    ({"equity": 48_400.0, "day_start_equity": 50_000.0, "peak_equity": 50_000.0}, "daily_loss_limit"),  # -3.2% today
    ({"equity": 44_000.0, "day_start_equity": 44_000.0, "peak_equity": 50_000.0}, "max_drawdown"),     # -12% from peak
    ({"trades_today": 10}, "trade_frequency"),
    ({"verified_at_ms": NOW - 120_000}, "account_state_fresh"),
    ({"open_risk": 500.0}, "open_risk_capacity"),  # combined exposure already at the 1% cap
])
def test_account_level_limits_reject(clear_switch, acct_kw, rule):
    acct = account(**{"equity": 50_000.0, "day_start_equity": 50_000.0, "peak_equity": 50_000.0, **acct_kw})
    assert rule in failed_rules(run(buy(3.0), acct, switch=clear_switch))


def test_stale_price_rejected(clear_switch):
    d = run(buy(3.0, price_time_ms=NOW - 5_000), account(50_000), switch=clear_switch)
    assert failed_rules(d) == {"price_fresh"}


def test_kill_switch_engaged_or_missing_rejects(tmp_path):
    ks = KillSwitch(tmp_path / "KILL_SWITCH")
    assert run(buy(3.0), account(50_000), switch=ks).approved
    ks.engage("test")
    assert "kill_switch" in failed_rules(run(buy(3.0), account(50_000), switch=ks))
    assert "kill_switch" in failed_rules(run(buy(3.0), account(50_000), switch=None))  # absent -> fail closed


def test_unverified_vault_spec_fails_closed(clear_switch):
    d = run(buy(3.0), account(50_000), spec=UNVERIFIED, switch=clear_switch)
    assert not d.approved and "broker_spec" in failed_rules(d)


@pytest.mark.parametrize("bad", [
    {"usd_to_account": math.nan}, {"usd_to_account": 0.0}, {"spread_usd": -0.1}, {"slippage_usd": math.inf},
])
def test_invalid_costs_fail_closed(clear_switch, bad):
    d = run(buy(3.0), account(50_000), costs=replace(COSTS, **bad), switch=clear_switch)
    assert not d.approved and "inputs_finite" in failed_rules(d)


def test_unsourced_costs_rejected(clear_switch):
    d = run(buy(3.0), account(50_000), costs=replace(COSTS, sources=()), switch=clear_switch)
    assert "costs_sourced" in failed_rules(d)


def test_there_is_no_live_mode(clear_switch):
    assert {m.value for m in Mode} == {"backtest", "paper"}
    d = run(buy(3.0), account(50_000), switch=clear_switch, mode="live")
    assert not d.approved and "mode" in failed_rules(d)


def test_margin_limit(clear_switch):
    d = run(buy(3.0), account(50_000, free_margin=100.0), switch=clear_switch)
    assert "margin" in failed_rules(d)


@pytest.mark.parametrize("field, value", [
    ("max_risk_per_trade_fraction", 0.05), ("max_daily_loss_fraction", 0.2), ("max_drawdown_fraction", 0.5),
    ("max_open_risk_fraction", 0.0), ("max_trades_per_day", 0), ("max_margin_utilisation", 0.9),
])
def test_limits_above_hard_ceilings_are_refused(field, value):
    with pytest.raises(RiskConfigError):
        replace(LIMITS, **{field: value})


def test_feasibility_table_for_r500():
    rows = feasibility(SPEC, 500.0, [0.5, 1.0, 3.0], COSTS, LIMITS)
    assert [round(r.risk_at_min_volume, 2) for r in rows] == [14.94, 23.24, 56.44]
    assert [r.permitted for r in rows] == [False, False, False]
