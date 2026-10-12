"""Live paper driver against a SIMULATED MT5 feed (any OS). Prices are synthetic; nothing reaches a broker."""

from __future__ import annotations

import math
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from atlas.backtest.costs import CostModel
from atlas.backtest.strategies import RandomEntry
from atlas.broker.live_paper import LivePaperDriver, server_epoch_to_utc_ms
from atlas.broker.mt5_readonly import BrokerReadError, Mt5ReadOnly
from atlas.broker.paper import ConnectionMonitor, PaperBroker
from atlas.broker.session import PaperSession
from atlas.brokers.server_time import ServerTimeError, ServerTimeModel
from atlas.brokers.spec import load_spec
from atlas.risk.gate import KillSwitch, RiskLimits
from atlas.sessions import XauCfd
from atlas.storage import Store

from .conftest import ROOT, utc_ms

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
COSTS = CostModel(0.30, 0.80, 15, 0.10, 16.6, "provisional_dev", ("test-only values",))
SERVER = ServerTimeModel("fixed:+03:00")
OFFSET_S = 3 * 3600
Account = namedtuple("Account", "login trade_mode leverage trade_allowed balance credit equity margin margin_free "
                                "margin_so_call margin_so_so currency server company")
Tick = namedtuple("Tick", "time bid ask")
T0 = utc_ms(2026, 7, 14, 9, 0)  # Tuesday, London session


def price_at(ms: int) -> float:
    return round(2400 + 3 * math.sin(ms / 600_000), 2)


class SimMt5:
    """Simulated terminal: quotes and closed M1 bars from a deterministic price path, times in SERVER time."""
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
    TIMEFRAME_M1, TIMEFRAME_M5 = 1, 5
    COPY_TICKS_ALL = -1

    def __init__(self, clock, server_offset_s=OFFSET_S):
        self.clock, self.offset = clock, server_offset_s
        self.down = False
        self.frozen_at = None

    def initialize(self):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (-10004, "no connection")

    def account_info(self):
        return Account(1, 0, 500, True, 1e6, 0, 1e6, 0, 1e6, 100, 50, "ZAR", "Sim-Demo", "Sim")

    def symbol_select(self, s, e):
        return True

    def symbol_info_tick(self, symbol):
        if self.down:
            return None
        t = self.frozen_at if self.frozen_at is not None else self.clock()
        bid = price_at(t)
        return Tick(t // 1000 + self.offset, bid, round(bid + 0.30, 2))

    def copy_rates_range(self, symbol, tf, start, end):
        if self.down:
            return None
        rows = []
        first = int(start.timestamp()) // 60 * 60
        for s in range(first, int(end.timestamp()) + 1, 60):
            pts = [price_at((s + k) * 1000) for k in range(0, 61, 15)]
            rows.append({"time": s + self.offset, "open": pts[0], "high": max(pts), "low": min(pts), "close": pts[-1],
                         "tick_volume": 10, "spread": 30})
        return rows


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def build(tmp_path, clock, model=SERVER, p=0.2):
    sim = SimMt5(clock)
    ro = Mt5ReadOnly(sim, clock=clock)
    ro.connect()
    store = Store(tmp_path / "live_paper.db")
    broker = PaperBroker(store, SPEC, kill_switch=KillSwitch(tmp_path / "KS"), slippage_usd=0.10)
    session = PaperSession(RandomEntry(seed=3, p=p, stop_usd=2.0, tp_usd=2.0), broker=broker, spec=SPEC,
                           limits=LIMITS, costs=COSTS, calendar=XauCfd(), initial_equity=1_000_000.0,
                           strategy_id="live-test", candles=[])
    driver = LivePaperDriver(ro, session, symbol="XAUUSD", timeframe="M1", server_time=model,
                             monitor=ConnectionMonitor(max_quote_age_ms=5_000), calendar=XauCfd(), clock=clock)
    return sim, driver, session, store


def run_for(driver, clock, minutes, step_ms=1_000):
    end = clock.t + minutes * 60_000
    while clock.t < end:
        driver.step()
        clock.t += step_ms


def test_server_epoch_conversion():
    assert server_epoch_to_utc_ms(T0 // 1000 + OFFSET_S, SERVER) == T0


def test_model_behind_the_server_is_detected_immediately(tmp_path):
    clock = Clock(T0)
    _, driver, _, store = build(tmp_path, clock, model=ServerTimeModel("fixed:+02:00"))  # really +03:00
    with pytest.raises(ServerTimeError, match="future"):
        driver.step()
    store.close()


def test_model_ahead_of_the_server_is_detected_within_minutes(tmp_path):
    clock = Clock(T0)
    _, driver, session, store = build(tmp_path, clock, model=ServerTimeModel("fixed:+04:00"), p=1.0)
    with pytest.raises(ServerTimeError, match="timestamps stay old"):
        run_for(driver, clock, minutes=15)
    assert session.rep.entries == []  # every quote looked stale: nothing was traded meanwhile
    store.close()


def test_quiet_closed_market_does_not_trip_the_clock_check(tmp_path):
    clock = Clock(utc_ms(2026, 7, 11, 12))  # Saturday: market closed, last quote is from Friday
    sim, driver, session, store = build(tmp_path, clock)
    sim.frozen_at = utc_ms(2026, 7, 10, 20, 59)
    run_for(driver, clock, minutes=20)
    assert session.rep.entries == [] and not driver.clock_verified
    store.close()


def test_live_paper_trades_on_closed_bars_and_live_quotes(tmp_path):
    clock = Clock(T0)
    sim, driver, session, store = build(tmp_path, clock)
    run_for(driver, clock, minutes=90)
    assert driver.clock_verified
    rep = session.rep
    assert len(session.candles) >= 85 and rep.entries, rep.events[:5]
    for c in session.candles:
        assert c.close_time <= T0 + 90 * 60_000  # only closed bars were ever appended
    for x in rep.entries:
        assert x["time"] > x["decision_time"] - 1 and x["time"] - x["decision_time"] <= 5 * 60_000
    ids = [r[0] for r in store.conn.execute("SELECT client_order_id FROM order_journal")]
    assert len(ids) == len(set(ids))
    store.close()


def test_no_trading_on_frozen_quotes(tmp_path):
    clock = Clock(T0)
    sim, driver, session, store = build(tmp_path, clock, p=1.0)
    run_for(driver, clock, minutes=3)
    sim.frozen_at = clock.t - 60_000  # feed stops updating: last quote ages
    before = (len(session.rep.entries), len(session.candles))
    run_for(driver, clock, minutes=10)
    assert (len(session.rep.entries), len(session.candles)) == before  # nothing acts on stale prices
    store.close()


def test_feed_outage_backs_off_and_recovers(tmp_path):
    clock = Clock(T0)
    sim, driver, session, store = build(tmp_path, clock)
    run_for(driver, clock, minutes=2)
    sim.down = True
    run_for(driver, clock, minutes=1, step_ms=500)
    errors = [e for e in driver.log if e[1] == "feed_error"]
    assert errors and driver.monitor.failures == len(errors) and len(errors) < 120  # backed off, not hammering
    sim.down = False
    run_for(driver, clock, minutes=3)
    assert driver.monitor.failures == 0 and driver.monitor.can_open_positions(clock.t)
    store.close()


def test_cli_paper_run_wiring_with_simulated_terminal(monkeypatch, tmp_path, capsys):
    from atlas import cli
    from atlas.broker import mt5_readonly
    clock = Clock(T0)
    monkeypatch.setattr(mt5_readonly, "load_mt5_module", lambda: SimMt5(clock))
    rc = cli.main(["--config", str(ROOT / "atlas.toml"), "paper-run", "--experiment",
                   "experiments/baseline_random_entry_sample.toml", "--minutes", "0",
                   "--server-time-model", "fixed:+03:00", "--kill-switch", str(tmp_path / "KS")])
    out = capsys.readouterr().out
    assert rc == 0 and "PAPER TRADING (simulated fills; no real orders)" in out and "Sim-Demo" in out
