"""Read-only MT5 adapter tests against a FAKE MetaTrader5 module (runs on any OS).

The fake mimics the documented return shapes (namedtuples with _asdict). Values are TEST-ONLY, not Vault facts.
Behaviour against a real terminal is verified separately on Windows (docs/14).
"""

from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone

import pytest

from atlas.broker.mt5_readonly import (
    BrokerReadError, Mt5ReadOnly, ReadOnlyViolation, load_mt5_module, spec_toml_from_snapshots,
)
from atlas.brokers.spec import SpecStatus, load_spec

Account = namedtuple("Account", "login trade_mode leverage trade_allowed balance credit equity margin margin_free "
                                "margin_so_call margin_so_so currency server company")
Symbol = namedtuple("Symbol", "name digits point trade_tick_size trade_tick_value trade_contract_size volume_min "
                              "volume_max volume_step trade_stops_level trade_freeze_level swap_long swap_short "
                              "swap_rollover3days currency_profit currency_margin trade_mode filling_mode spread "
                              "spread_float")
Tick = namedtuple("Tick", "time bid ask")
Position = namedtuple("Position", "ticket symbol type volume price_open sl tp magic comment")


class FakeMt5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
    TIMEFRAME_M1, TIMEFRAME_M5 = 1, 5
    COPY_TICKS_ALL = -1

    def __init__(self, trade_mode=0, trade_allowed=True, init_ok=True, quote=(2400.10, 2400.40)):
        self.trade_mode, self.trade_allowed, self.init_ok, self.quote = trade_mode, trade_allowed, init_ok, quote
        self.calls = []
        self.init_kwargs = None
        self.sent_orders = []

    def initialize(self, *args, **kwargs):
        self.init_kwargs = (args, kwargs)
        return self.init_ok

    def shutdown(self):
        self.calls.append("shutdown")

    def last_error(self):
        return (1, "test error")

    def account_info(self):
        return Account(12345, self.trade_mode, 500, self.trade_allowed, 500.0, 0.0, 500.0, 0.0, 500.0,
                       100.0, 50.0, "ZAR", "Test-Server", "Test Co")

    def symbol_select(self, symbol, enable):
        self.calls.append(("symbol_select", symbol))
        return True

    def symbol_info(self, symbol):
        if symbol != "XAUUSD":
            return None
        return Symbol("XAUUSD", 2, 0.01, 0.01, 1.66, 100.0, 0.01, 50.0, 0.01, 30, 5, -25.0, 10.0, 3, "USD", "USD",
                      4, 3, 25, True)

    def symbol_info_tick(self, symbol):
        return Tick(1_791_700_000, *self.quote)

    def positions_get(self, symbol=None):
        return [Position(1, "XAUUSD", 0, 0.01, 2400.0, 2395.0, 2410.0, 7, "atlas")]

    def history_deals_get(self, start, end):
        return getattr(self, "deals_rows", [])

    def copy_rates_range(self, symbol, tf, start, end):
        return [{"time": 1_791_700_000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 10,
                 "spread": 20}]

    # trading functions exist on the real module; the adapter must never reach them
    def order_send(self, request):
        self.sent_orders.append(request)
        return None

    def order_check(self, request):
        return None


def test_connect_passes_no_credentials_and_reads_account():
    fake = FakeMt5()
    acc = Mt5ReadOnly(fake).connect()
    assert fake.init_kwargs == ((), {})  # no path, login, password or server: the terminal's saved login is used
    assert (acc.trade_mode, acc.currency, acc.margin_so_so) == ("demo", "ZAR", 50.0)


def test_real_account_with_trading_enabled_is_refused():
    fake = FakeMt5(trade_mode=2, trade_allowed=True)
    with pytest.raises(ReadOnlyViolation, match="INVESTOR"):
        Mt5ReadOnly(fake).connect()
    assert "shutdown" in fake.calls


def test_real_account_with_investor_login_is_accepted():
    acc = Mt5ReadOnly(FakeMt5(trade_mode=2, trade_allowed=False)).connect()
    assert acc.trade_mode == "real" and acc.trade_allowed is False


def test_unknown_trade_mode_fails_closed():
    with pytest.raises(BrokerReadError, match="trade mode"):
        Mt5ReadOnly(FakeMt5(trade_mode=99)).connect()


def test_failed_initialize_is_an_error():
    with pytest.raises(BrokerReadError, match="initialize"):
        Mt5ReadOnly(FakeMt5(init_ok=False)).connect()


@pytest.mark.parametrize("name", ["order_send", "order_check", "login", "positions_total", "TRADE_ACTION_DEAL"])
def test_trading_and_unlisted_functions_are_unreachable(name):
    fake = FakeMt5()
    ro = Mt5ReadOnly(fake)
    with pytest.raises(ReadOnlyViolation):
        getattr(ro.mt5, name)
    with pytest.raises(ReadOnlyViolation):
        ro.mt5.order_send = lambda r: None  # cannot be re-wired either
    assert fake.sent_orders == []


def test_symbol_tick_positions_snapshots():
    ro = Mt5ReadOnly(FakeMt5())
    s = ro.symbol_snapshot("XAUUSD")
    assert (s.contract_size, s.volume_min, s.volume_step, s.stops_level_points, s.freeze_level_points) == \
           (100.0, 0.01, 0.01, 30, 5)
    t = ro.tick("XAUUSD")
    assert t.spread == pytest.approx(0.30)
    (p,) = ro.positions("XAUUSD")
    assert (p.side, p.volume, p.magic) == ("buy", 0.01, 7)
    with pytest.raises(BrokerReadError, match="not found"):
        ro.symbol_snapshot("XAUUSD.m")


@pytest.mark.parametrize("quote", [(2400.4, 2400.1), (0.0, 2400.0), (float("nan"), 2400.0)])
def test_invalid_quotes_rejected(quote):
    with pytest.raises(BrokerReadError, match="invalid quote"):
        Mt5ReadOnly(FakeMt5(quote=quote)).tick("XAUUSD")


def test_live_snapshots_render_a_verified_spec_usable_by_the_risk_gate(tmp_path):
    ro = Mt5ReadOnly(FakeMt5(), clock=lambda: int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp() * 1000))
    acc = ro.connect()
    text = spec_toml_from_snapshots(ro.symbol_snapshot("XAUUSD"), acc, broker="Test Broker",
                                    server_time_model="ny_close_midnight", source_note="unit test (fake terminal)")
    f = tmp_path / "spec.toml"
    f.write_text(text.replace("# commission is NOT available", "commission_per_lot_round_turn = 0.0\n# commission is NOT available"),
                 encoding="utf-8")
    spec = load_spec(f)
    assert spec.status is SpecStatus.VERIFIED and spec.server == "Test-Server"
    assert spec.account.stop_out_level_pct == 50.0 and spec.symbol.stops_level_points == 30
    spec.require(allowed={SpecStatus.VERIFIED})


def test_spec_without_commission_is_incomplete(tmp_path):
    ro = Mt5ReadOnly(FakeMt5())
    acc = ro.connect()
    f = tmp_path / "spec.toml"
    f.write_text(spec_toml_from_snapshots(ro.symbol_snapshot("XAUUSD"), acc, broker="T", server_time_model="fixed:+02:00",
                                          source_note="t"), encoding="utf-8")
    from atlas.brokers.spec import SpecError
    with pytest.raises(SpecError, match="commission"):  # never assumed: must come from deals or support
        load_spec(f)


def test_package_loader_refuses_off_windows(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(BrokerReadError, match="Windows"):
        load_mt5_module()


def test_cli_mt5_check_with_fake_terminal(monkeypatch, tmp_path, capsys):
    from atlas import cli
    from atlas.broker import mt5_readonly
    fake = FakeMt5(trade_mode=2, trade_allowed=False)
    monkeypatch.setattr(mt5_readonly, "load_mt5_module", lambda: fake)
    (tmp_path / "atlas.toml").write_text('[scope]\nauthorised_instruments = ["XAUUSD"]\n', encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.toml").write_text(
        '[[instrument]]\nid = "XAUUSD"\nasset_class = "metal_spot"\nbase = "XAU"\nquote = "USD"\n'
        'session_calendar = "xau_cfd"\n', encoding="utf-8")
    rc = cli.main(["--config", str(tmp_path / "atlas.toml"), "mt5-check", "--write-spec", "spec_live.toml"])
    out = capsys.readouterr().out
    assert rc == 0 and "***345" in out and "12345" not in out  # account number masked
    assert "mode=real" in out and "trading allowed in this session=False" in out
    assert (tmp_path / "spec_live.toml").exists() and fake.sent_orders == []


def test_cli_mt5_check_refuses_trading_enabled_real_account(monkeypatch, tmp_path, capsys):
    from atlas import cli
    from atlas.broker import mt5_readonly
    monkeypatch.setattr(mt5_readonly, "load_mt5_module", lambda: FakeMt5(trade_mode=2, trade_allowed=True))
    (tmp_path / "atlas.toml").write_text('[scope]\nauthorised_instruments = ["XAUUSD"]\n', encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.toml").write_text(
        '[[instrument]]\nid = "XAUUSD"\nasset_class = "metal_spot"\nbase = "XAU"\nquote = "USD"\n'
        'session_calendar = "xau_cfd"\n', encoding="utf-8")
    assert cli.main(["--config", str(tmp_path / "atlas.toml"), "mt5-check"]) == 2
    assert "INVESTOR" in capsys.readouterr().err


Deal = namedtuple("Deal", "symbol entry volume commission")


def test_commission_from_deals():
    from atlas.broker.mt5_readonly import commission_from_deals
    deals = [Deal("XAUUSD", 0, 0.10, -0.35), Deal("XAUUSD", 1, 0.10, -0.35), Deal("EURUSD", 0, 1.0, -7.0)]
    est = commission_from_deals(deals, "XAUUSD", "ZAR")
    assert est.per_lot_round_turn == pytest.approx(7.0) and est.deals_used == 2  # (0.35 + 0.35) / 0.10 lot
    none = commission_from_deals([], "XAUUSD", "ZAR")
    assert none.per_lot_round_turn is None and "demo trade" in none.note  # never assumed
    weird = commission_from_deals([Deal("XAUUSD", 0, 0.1, 1.0)], "XAUUSD", "ZAR")
    assert weird.per_lot_round_turn is None and "manual review" in weird.note


def test_history_deals_is_on_the_read_allowlist():
    ro = Mt5ReadOnly(FakeMt5())
    assert ro.deals(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)) == []
