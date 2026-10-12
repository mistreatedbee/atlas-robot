"""Paper execution tests (simulated only; PROVISIONAL dev spec; test-only quotes)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from atlas.broker.mt5_readonly import TickSnapshot
from atlas.broker.orders import Target, prepare_order
from atlas.broker.paper import ConnectionMonitor, DuplicateOrder, OrderRejected, PaperBroker
from atlas.brokers.spec import load_spec
from atlas.risk.gate import Check, KillSwitch, Mode, RiskDecision
from atlas.storage import Store

from .conftest import ROOT

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
NOW = 1_800_000_000_000
APPROVED = RiskDecision(True, 0.05, 100.0, 0.01, (Check("x", True, ""),), Mode.PAPER, "s", ("t",))


def quote(bid, ask=None):
    return TickSnapshot("XAUUSD", 0, bid, ask if ask is not None else round(bid + 0.30, 2))


def order(side="buy", t=NOW, tick=None, decision_time=NOW - 1000, **kw):
    p = prepare_order(target=Target.PAPER, spec=SPEC, decision=APPROVED, side=side, stop_distance=3.0,
                      tp_distance=4.5, tick=tick or quote(2400.10), quote_time_ms=t, now_ms=t, strategy_id="S1",
                      decision_time_ms=decision_time, **kw)
    assert p.ok, p.failed
    return p.order


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "paper.db")
    ks = KillSwitch(tmp_path / "KILL_SWITCH")
    yield store, ks, PaperBroker(store, SPEC, kill_switch=ks, slippage_usd=0.10)
    store.close()


def test_fill_and_take_profit(env):
    store, ks, b = env
    o = order()
    pos = b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert (pos.side, pos.entry_price, pos.volume) == ("buy", 2400.50, 0.05)  # ask 2400.40 + slip 0.10
    assert b.on_tick(quote(2404.00), now_ms=NOW + 1) == []
    (c,) = b.on_tick(quote(2405.00), now_ms=NOW + 2)
    assert (c.exit_reason, c.exit_price) == ("tp", o.tp)
    assert c.gross_usd == pytest.approx((o.tp - 2400.50) * 100 * 0.05)
    assert b.journal_status(o.client_order_id) == "filled" and b.open_positions() == []


def test_stop_fills_at_worse_of_stop_and_market(env):
    _, _, b = env
    o = order()
    b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    (c,) = b.on_tick(quote(2390.00), now_ms=NOW + 1)  # gapped far through the stop
    assert c.exit_reason == "stop" and c.exit_price == pytest.approx(2390.00 - 0.10)


def test_short_uses_ask_for_stop(env):
    _, _, b = env
    o = order(side="sell")
    pos = b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert pos.entry_price == pytest.approx(2400.00)  # bid - slip
    (c,) = b.on_tick(quote(o.sl - 0.20, o.sl + 0.10), now_ms=NOW + 1)  # ask crosses the stop
    assert c.exit_reason == "stop" and c.exit_price == pytest.approx(o.sl + 0.10 + 0.10)


def test_duplicate_order_refused_even_after_restart(env, tmp_path):
    store, ks, b = env
    o = order()
    b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    with pytest.raises(DuplicateOrder):
        b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    store.close()
    store2 = Store(tmp_path / "paper.db")  # process restart
    b2 = PaperBroker(store2, SPEC, kill_switch=ks, slippage_usd=0.10)
    with pytest.raises(DuplicateOrder):
        b2.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    (pos,) = b2.open_positions()  # restart recovery: the position is still managed
    assert pos.client_order_id == o.client_order_id
    assert b2.on_tick(quote(2405.0), now_ms=NOW + 5)[0].exit_reason == "tp"
    store2.close()


def test_rejected_attempt_is_journalled_and_cannot_be_retried_as_new(env):
    _, _, b = env
    o = order()
    with pytest.raises(OrderRejected, match="stale_quote"):
        b.submit(o, quote(2400.10), quote_time_ms=NOW - 10_000, now_ms=NOW)
    assert b.journal_status(o.client_order_id) == "rejected"
    with pytest.raises(DuplicateOrder):
        b.submit(o, quote(2400.10), quote_time_ms=NOW, now_ms=NOW)


@pytest.mark.parametrize("kw, reason", [
    ({"now_ms": NOW + 60_000, "quote_time_ms": NOW + 60_000}, "expired"),
    ({"tick": quote(2401.50)}, "price_moved"),                 # market moved 1.40 > 20 points
    ({"tick": TickSnapshot("XAUUSD", 0, 2400.5, 2400.1)}, "invalid_quote"),
])
def test_fail_closed_rejections(env, kw, reason):
    _, _, b = env
    args = {"tick": quote(2400.10), "quote_time_ms": NOW, "now_ms": NOW, **kw}
    with pytest.raises(OrderRejected) as e:
        b.submit(order(), args["tick"], quote_time_ms=args["quote_time_ms"], now_ms=args["now_ms"])
    assert e.value.reason == reason


def test_one_combined_exposure(env):
    _, _, b = env
    b.submit(order(), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    with pytest.raises(OrderRejected) as e:
        b.submit(order(decision_time=NOW - 500), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert e.value.reason == "position_open"


def test_kill_switch_blocks_and_emergency_stop_flattens(env):
    store, ks, b = env
    b.submit(order(), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    closed = b.emergency_stop(quote(2399.00), reason="test", now_ms=NOW + 1)
    assert ks.engaged() and len(closed) == 1 and closed[0].exit_price == pytest.approx(2398.90)
    with pytest.raises(OrderRejected) as e:
        b.submit(order(decision_time=NOW - 7), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert e.value.reason == "kill_switch"
    kinds = [r[0] for r in store.conn.execute("SELECT kind FROM paper_events ORDER BY event_id")]
    assert kinds[:3] == ["filled", "emergency_stop", "closed"]


def test_emergency_stop_without_quote_keeps_stops_and_still_engages(env):
    _, ks, b = env
    b.submit(order(), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert b.emergency_stop(None, reason="feed lost", now_ms=NOW + 1) == []
    assert ks.engaged() and len(b.open_positions()) == 1


def test_bad_quote_never_triggers_exits(env):
    _, _, b = env
    b.submit(order(), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    assert b.on_tick(TickSnapshot("XAUUSD", 0, 0.0, 0.0), now_ms=NOW + 1) == []
    assert len(b.open_positions()) == 1


def test_journal_and_event_log_are_permanent(env):
    store, _, b = env
    b.submit(order(), quote(2400.10), quote_time_ms=NOW, now_ms=NOW)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM order_journal")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE paper_events SET kind='x'")


def test_connection_monitor_fails_closed_and_backs_off():
    m = ConnectionMonitor(max_quote_age_ms=2_000, base_backoff_ms=1_000, max_backoff_ms=8_000)
    assert not m.can_open_positions(NOW)  # never seen a quote
    m.record_quote(NOW)
    assert m.can_open_positions(NOW + 1_000) and not m.can_open_positions(NOW + 3_000)  # stale
    delays = [m.record_failure(NOW) for _ in range(5)]
    assert delays == [1_000, 2_000, 4_000, 8_000, 8_000]
    assert not m.can_open_positions(NOW) and not m.may_attempt(NOW + 7_999) and m.may_attempt(NOW + 8_000)
    m.record_quote(NOW + 9_000)
    assert m.can_open_positions(NOW + 9_500)


def test_paper_slippage_must_be_at_least_one_tick(tmp_path):
    with pytest.raises(ValueError):
        PaperBroker(Store(tmp_path / "x.db"), SPEC, kill_switch=KillSwitch(tmp_path / "k"), slippage_usd=0.0)
