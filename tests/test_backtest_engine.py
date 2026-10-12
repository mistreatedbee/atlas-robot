"""Backtest engine tests against hand-computed examples (docs/13 v2).

All prices are SYNTHETIC test fixtures. Costs/spec are PROVISIONAL dev values. Hand calculation conventions:
spread 0.30, reopen spread 0.80 (15 min), slippage 0.10 USD/oz, 100 oz/lot, USD->account 10, equity 100,000,
risk limit 1% -> 1,000 per trade.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from atlas.backtest import engine as engine_mod
from atlas.backtest.costs import CostModel, CostModelError
from atlas.backtest.engine import EngineConfig, Signal, run_backtest
from atlas.backtest.metrics import compute_metrics
from atlas.brokers.spec import load_spec
from atlas.models import PriceBasis
from atlas.risk.gate import RiskLimits
from atlas.sessions import XauCfd

from .conftest import ROOT, make_candle, utc_ms

SPEC = load_spec(ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml")
LIMITS = RiskLimits.load(ROOT / "config/risk.toml")
COSTS = CostModel(base_spread_usd=0.30, reopen_spread_usd=0.80, reopen_minutes=15, slippage_usd=0.10,
                  usd_to_account=10.0, status="provisional_dev", sources=("test-only values",))
M5 = 300_000
T0 = utc_ms(2026, 7, 14, 10, 0)  # Tuesday, mid London session


def bar(t, o, h, l, c, **kw):
    return make_candle(t, tf="M5", instrument_id="XAUUSD", open=o, high=h, low=l, close=c,
                       price_basis=PriceBasis.BID, **kw)


class Scripted:
    name = "scripted"
    hypothesis_id = "TEST-scripted"

    def __init__(self, signals):
        self.signals = signals

    def params(self):
        return {}

    def prepare(self, candles):
        pass

    def decide(self, i):
        return self.signals.get(i)


def run(candles, signals, equity=100_000.0, costs=COSTS, **cfg):
    return run_backtest(candles, Scripted(signals), spec=SPEC, limits=LIMITS, costs=costs, calendar=XauCfd(),
                        config=EngineConfig(initial_equity=equity, **cfg))


def base_bars():
    return [
        bar(T0, 2400.0, 2400.5, 2399.5, 2400.0),           # 0: decision bar
        bar(T0 + M5, 2400.0, 2401.0, 2399.8, 2400.8),      # 1: fill bar
    ]


BUY = {0: Signal("buy", 2.0, 3.0)}
SELL = {0: Signal("sell", 2.0, 3.0)}


def test_long_take_profit_matches_hand_calculation():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.41, 2400.9, 2403.0)]
    r = run(bars, BUY)
    (t,) = r.trades
    # v2.4: ask = 2400.00 + 0.30 = 2400.30 ; fill = ask + slip = 2400.40 ; stop = ask - 2 = 2398.30 ; tp = 2403.30
    assert (t.entry_time, t.entry_price, t.stop, t.tp) == (T0 + M5, 2400.40, 2398.30, 2403.30)
    # gate: risk distance from fill 2.10 ; per lot = (2.10 + 0.30 + 0.10) x 100 x 10 = 2500 ; 1000 / 2500 -> 0.40
    assert t.lots == pytest.approx(0.40) and t.planned_risk == pytest.approx(1000.0)
    assert (t.exit_reason, t.exit_price) == ("tp", 2403.30)
    assert t.gross_usd == pytest.approx(116.0)           # 2.90 x 100 x 0.40
    assert t.net_account == pytest.approx(1160.0)
    assert t.spread_cost_usd == pytest.approx(12.0) and t.slippage_cost_usd == pytest.approx(4.0)
    # R unit = (2.10 + 0.10 slippage) x 100 oz x 0.40 x 10 = 880
    assert t.r_multiple == pytest.approx(1160 / 880)
    assert r.equity_curve[-1][1] == pytest.approx(101_160.0)


def test_take_profit_requires_trading_through_by_one_tick():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.30, 2400.9, 2403.0)]  # touches tp 2403.30, not through
    r = run(bars, BUY)
    assert r.trades[0].exit_reason == "end_of_data"


def test_both_levels_in_one_bar_assumes_stop_first():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.50, 2398.0, 2402.0)]
    (t,) = run(bars, BUY).trades
    assert (t.exit_reason, t.exit_price) == ("stop", 2398.20)  # stop 2398.30 - slippage
    assert t.net_account == pytest.approx((2398.20 - 2400.40) * 40 * 10)
    assert t.r_multiple == pytest.approx(-1.0)  # a clean stop-out is exactly -1R (review finding 8)


def test_gap_through_stop_fills_at_open_and_r_is_not_capped():
    bars = base_bars() + [bar(T0 + 2 * M5, 2397.0, 2397.5, 2396.5, 2397.2)]
    (t,) = run(bars, BUY).trades
    assert (t.exit_reason, t.exit_price) == ("stop_gap", 2396.90)
    assert t.net_account == pytest.approx(-1400.0)          # (2396.90 - 2400.40) x 40 oz x 10
    assert t.r_multiple == pytest.approx(-1400 / 880) and t.r_multiple < -1


def test_gap_through_take_profit_fills_at_tp_only():
    bars = base_bars() + [bar(T0 + 2 * M5, 2405.0, 2406.0, 2404.5, 2405.5)]
    (t,) = run(bars, BUY).trades
    assert (t.exit_reason, t.exit_price) == ("tp_gap", 2403.30)


def test_short_uses_ask_for_stop_and_take_profit():
    # v2.4: fill = bid - slip = 2399.90 ; stop = bid + 2 = 2402.00 ; tp = bid - 3 = 2397.00 ; ask = bid + 0.30
    tp_bars = base_bars() + [bar(T0 + 2 * M5, 2399.0, 2400.0, 2396.59, 2397.0)]  # ask_low 2396.89 <= tp - tick
    (t,) = run(tp_bars, SELL).trades
    assert (t.entry_price, t.stop, t.tp, t.exit_reason, t.exit_price) == (2399.90, 2402.00, 2397.00, "tp", 2397.00)
    assert t.lots == pytest.approx(0.40)
    assert t.gross_usd == pytest.approx(116.0) and t.spread_cost_usd == pytest.approx(12.0)
    stop_bars = base_bars() + [bar(T0 + 2 * M5, 2400.0, 2401.70, 2399.5, 2400.0)]  # ask_high 2402.00 = stop
    (s,) = run(stop_bars, SELL).trades
    assert (s.exit_reason, s.exit_price) == ("stop", 2402.10)


def test_stop_and_tp_rounded_to_ticks_conservatively():
    # long from ask 2400.30: stop 2.005 -> 2398.295 rounded DOWN (away) to 2398.29 ; tp 3.005 -> 2403.305 DOWN to 2403.30
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2401.5, 2400.9, 2401.0)]
    (t,) = run(bars, {0: Signal("buy", 2.005, 3.005)}).trades
    assert (t.stop, t.tp) == (2398.29, 2403.30)


def test_fill_is_next_bar_never_signal_bar_and_latency_delays_it():
    bars = base_bars() + [bar(T0 + 2 * M5, 2410.0, 2410.5, 2409.5, 2410.0)]
    assert run(bars, BUY).trades[0].entry_time == T0 + M5
    late = run(bars, BUY, latency_bars=1).trades[0]
    assert late.entry_time == T0 + 2 * M5 and late.entry_price == pytest.approx(2410.40)


def test_missing_bars_expire_orders_beyond_the_fill_wait():
    bars = [bar(T0, 2400, 2400.5, 2399.5, 2400), bar(T0 + 4 * M5, 2400, 2401, 2399, 2400)]  # 15 min of no bars
    r = run(bars, BUY)
    assert r.trades == [] and [e.kind for e in r.events].count("expired_wait") == 1
    short_gap = [bar(T0, 2400, 2400.5, 2399.5, 2400), bar(T0 + 2 * M5, 2400, 2401, 2399.9, 2400)]  # 5 min gap
    assert run(short_gap, BUY).trades[0].entry_time == T0 + 2 * M5  # within max_fill_wait


def test_session_boundaries_block_entries_force_exits_and_cancel_across_closures():
    t = utc_ms(2026, 7, 14, 20, 40)  # daily break 21:00-22:00 UTC in July
    pre = [bar(t + k * M5, 2400, 2400.5, 2399.5, 2400) for k in range(4)]  # 20:40 .. 20:55
    post = [bar(utc_ms(2026, 7, 14, 22) + k * M5, 2400, 2400.5, 2399.5, 2400) for k in range(5)]  # 22:00 ..
    bars = pre + post
    r = run(bars, {0: Signal("buy", 5.0, None), 2: Signal("buy", 5.0, None), 3: Signal("buy", 5.0, None),
                   4: Signal("buy", 5.0, None)})
    kinds = [(e.time, e.kind) for e in r.events if e.kind != "signal"]
    (tr,) = r.trades
    assert tr.entry_time == t + M5 and tr.exit_reason == "session_close" and tr.exit_time == t + 3 * M5  # 20:55
    assert (t + 3 * M5, "blocked_window") in kinds          # fill at 20:55 would sit in the 5-min pre-close buffer
    assert (utc_ms(2026, 7, 14, 22), "expired_closure") in kinds  # decided 21:00, closure before next bar
    assert (utc_ms(2026, 7, 14, 22, 5), "blocked_window") in kinds  # reopen window (15 min)


def test_reopen_spread_is_wider():
    c = bar(utc_ms(2026, 7, 14, 22, 0), 2400, 2400.5, 2399.5, 2400)
    assert COSTS.spread_at(c, XauCfd(), 0.01) == 0.80
    assert COSTS.spread_at(replace(c, spread_points=95), XauCfd(), 0.01) == pytest.approx(0.95)  # bar max wins
    assert COSTS.spread_at(bar(T0, 2400, 2400.5, 2399.5, 2400), XauCfd(), 0.01) == 0.30


def test_duplicate_and_overlapping_signals_are_ignored():
    bars = base_bars() + [bar(T0 + k * M5, 2400.5, 2401, 2400, 2400.5) for k in range(2, 5)]
    r = run(bars, {0: Signal("buy", 2.0, 30.0), 1: Signal("sell", 2.0, 3.0), 2: Signal("buy", 2.0, 3.0)})
    assert len(r.trades) == 1
    assert [e.kind for e in r.events].count("ignored_in_position") == 2


def test_pending_order_blocks_second_signal():
    bars = base_bars() + [bar(T0 + 2 * M5, 2400.5, 2401, 2400, 2400.5)]
    r = run(bars, {0: Signal("buy", 2.0, 3.0)}, latency_bars=1)
    r2 = run(bars, {0: Signal("buy", 2.0, 3.0), 1: Signal("sell", 2.0, 3.0)}, latency_bars=1)
    assert [e.kind for e in r2.events].count("ignored_pending") == 1
    assert len(r.trades) == len(r2.trades) == 1


def test_r500_account_trades_are_rejected_by_the_gate():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.41, 2400.9, 2403.0)]
    r = run(bars, BUY, equity=500.0, costs=replace(COSTS, usd_to_account=16.6))
    assert r.trades == []
    (rej,) = [e for e in r.events if e.kind == "risk_rejected"]
    assert "minimum_volume_within_risk_limit" in rej.detail


def test_every_entry_goes_through_the_risk_gate(monkeypatch):
    calls = []
    real = engine_mod.evaluate

    def spy(*args, **kwargs):
        d = real(*args, **kwargs)
        calls.append(d.approved)
        return d

    monkeypatch.setattr(engine_mod, "evaluate", spy)
    bars = base_bars() + [bar(T0 + k * M5, 2400, 2400.6, 2399.9, 2400.2) for k in range(2, 12)]
    signals = {k: Signal("buy", 0.5, 0.5) for k in range(0, 11)}
    r = run(bars, signals)
    assert len(calls) == len(r.trades) + sum(e.kind == "risk_rejected" for e in r.events) > 0
    r_killed = run(bars, BUY, kill_switch_engaged=True)
    assert r_killed.trades == [] and "kill_switch" in r_killed.events[-1].detail


def test_unexpected_data_gap_is_held_not_anticipated():
    bars = base_bars() + [bar(T0 + 2 * M5, 2400.5, 2401, 2400, 2400.5),
                          bar(T0 + 6 * M5, 2396.0, 2396.5, 2395.5, 2396.0)]  # 15 min of missing data mid-session
    (t,) = run(bars, BUY).trades
    assert t.exit_reason == "stop_gap" and t.held_through_gap and t.exit_price == pytest.approx(2395.90)


def test_max_hold_and_end_of_data_exits():
    bars = base_bars() + [bar(T0 + k * M5, 2400.5, 2401, 2400, 2400.5) for k in range(2, 6)]
    assert run(bars, BUY, max_hold_bars=2).trades[0].exit_reason == "max_hold"
    t = run(bars, BUY).trades[0]
    assert t.exit_reason == "end_of_data" and t.exit_price == pytest.approx(2400.40)  # bid close - slip


def test_commission_charged_half_each_side(tmp_path):
    text = (ROOT / "config/brokers/PROVISIONAL_dev_xauusd.toml").read_text(encoding="utf-8").replace(
        "commission_per_lot_round_turn = 0.0", "commission_per_lot_round_turn = 7.0")
    (tmp_path / "s.toml").write_text(text, encoding="utf-8")
    spec = load_spec(tmp_path / "s.toml")
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.41, 2400.9, 2403.0)]
    r = run_backtest(bars, Scripted(BUY), spec=spec, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                     config=EngineConfig(initial_equity=100_000.0))
    (t,) = r.trades
    # per lot = 2500 + 7 x 10 = 2570 -> 0.38 lots ; commission 7 x 0.38 = 2.66 USD
    assert t.lots == pytest.approx(0.38) and t.commission_usd == pytest.approx(2.66)
    assert t.net_account == pytest.approx((2.90 * 100 * 0.38 - 2.66) * 10)
    assert r.equity_curve[-1][1] == pytest.approx(100_000 + t.net_account)


def test_cost_model_validation():
    with pytest.raises(CostModelError, match="one tick"):
        replace(COSTS, slippage_usd=0.0).validate(0.01)
    with pytest.raises(CostModelError):
        COSTS.scaled(0.5)
    s = COSTS.scaled(2.0)
    assert (s.base_spread_usd, s.slippage_usd, s.usd_to_account) == (0.60, 0.20, 10.0)


def test_metrics_definitions():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.41, 2400.9, 2403.0)]
    m = compute_metrics(run(bars, BUY))
    assert m["trades"] == 1 and m["win_rate"] == 1.0
    assert m["profit_factor"] == "undefined (0 losses)"
    assert m["net_pnl"] == pytest.approx(1160.0) and m["net_pnl_usd"] == pytest.approx(116.0)
    assert m["sharpe"] is None and "trading days" in m["sharpe_note"]
    assert m["costs_per_trade_r"] == pytest.approx((12.0 + 4.0) * 10 / 880)
    loss_bars = base_bars() + [bar(T0 + 2 * M5, 2397.0, 2397.5, 2396.5, 2397.2)]
    ml = compute_metrics(run(loss_bars, BUY))
    # mark-to-market peak includes bar 1's open gain: (2400.80 - 2400.40) x 40 oz x 10 = 160
    assert ml["max_drawdown"] == pytest.approx(1400.0 + 160.0) and ml["drawdown_unrecovered_at_end"] is True
    assert ml["avg_loss"] == pytest.approx(-1400.0) and ml["avg_win"] is None


# ---- review findings (backtester) ---------------------------------------------------------------------

def test_finding_1_missing_pre_close_bars_cannot_carry_a_position_through_the_break():
    # last bar 20:40-20:45 UTC, then nothing until 22:00 (daily break 21:00-22:00): the session exit never fires
    bars = [bar(utc_ms(2026, 7, 14, 20, 35), 2400, 2400.5, 2399.5, 2400),
            bar(utc_ms(2026, 7, 14, 20, 40), 2400, 2400.5, 2399.6, 2400),
            bar(utc_ms(2026, 7, 14, 22, 0), 2395, 2395.5, 2394.5, 2395),
            bar(utc_ms(2026, 7, 14, 22, 5), 2395, 2395.5, 2394.5, 2395)]
    (t,) = run(bars, {0: Signal("buy", 10.0, None)}).trades
    assert t.exit_reason == "held_through_closure" and t.held_through_gap
    assert t.exit_time == utc_ms(2026, 7, 14, 22, 0) and t.exit_price == pytest.approx(2394.90)  # open - slip


def test_finding_3_non_bid_bars_rejected():
    bars = [make_candle(T0, tf="M5", instrument_id="XAUUSD"), make_candle(T0 + M5, tf="M5", instrument_id="XAUUSD")]
    with pytest.raises(ValueError, match="BID"):
        run(bars, {})


def test_finding_7_stop_inside_the_spread_is_hit_at_the_open():
    # buy fill 2400.40 with a 0.20 stop -> stop 2400.20 > bid open 2400.00: exits at once at bid open - slip
    bars = base_bars() + [bar(T0 + 2 * M5, 2400.5, 2401, 2400, 2400.5)]
    (t,) = run(bars, {0: Signal("buy", 0.20, None)}).trades
    assert (t.exit_reason, t.exit_time, t.exit_price) == ("stop_gap", T0 + M5, 2399.90)


def test_finding_11_warm_up_bars_do_not_enter_results():
    bars = [bar(T0 + k * M5, 2400, 2400.5, 2399.5, 2400) for k in range(10)]
    r = run_backtest(bars, Scripted({}), spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                     config=EngineConfig(initial_equity=100_000.0), trade_start_ms=T0 + 6 * M5)
    assert r.bars_total == 4 and r.equity_curve[0][0] == T0 + 7 * M5


def test_recheck_B_closure_exit_is_capped_at_take_profit():
    bars = [bar(utc_ms(2026, 7, 14, 20, 35), 2400, 2400.5, 2399.5, 2400),
            bar(utc_ms(2026, 7, 14, 20, 40), 2400, 2400.5, 2399.6, 2400),
            bar(utc_ms(2026, 7, 14, 22, 0), 2420, 2420.5, 2419.5, 2420)]   # gaps far beyond the TP
    (t,) = run(bars, {0: Signal("buy", 10.0, 5.0)}).trades
    assert (t.exit_reason, t.exit_price) == ("tp_gap", t.tp)


# ---- news windows and point-in-time FX (rules v2.3) ------------------------------------------------------

from atlas.backtest.engine import NewsRules  # noqa: E402
from atlas.events import EconEvent, EventCalendar  # noqa: E402
from atlas.fx import FxSeries  # noqa: E402

MIN = 60_000


def cal_with(release, known_since=None):
    return EventCalendar([EconEvent("CPI-test", "CPI", 1, "x", release,
                                    known_since if known_since is not None else release - 86_400_000, "test", "")])


def run_news(bars, signals, calendar, rules=NewsRules(), fx=None, equity=100_000.0):
    return run_backtest(bars, Scripted(signals), spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                        config=EngineConfig(initial_equity=equity), events_calendar=calendar, news=rules,
                        fx_series=fx)


def flat_bars(n, start=T0, price=2400.0):
    return [bar(start + k * M5, price, price + 0.5, price - 0.5, price) for k in range(n)]


def test_news_window_blocks_entries_only_for_known_events():
    bars = flat_bars(6)
    release = T0 + M5 + 10 * MIN  # fill bar opens 10 min before the release -> inside the 15-min window
    r = run_news(bars, {0: Signal("buy", 2.0, 3.0)}, cal_with(release))
    assert r.trades == [] and [e.detail for e in r.events if e.kind == "blocked_news"] == ["CPI-test"]
    # the same release, but only "known" after the fill time: not blocked (point-in-time)
    r2 = run_news(bars, {0: Signal("buy", 2.0, 3.0)}, cal_with(release, known_since=T0 + 2 * M5))
    assert len(r2.trades) == 1


def test_flat_before_news_and_news_slippage():
    bars = flat_bars(12)
    release = T0 + 9 * M5  # 10:45; the window opens at 10:30
    r = run_news(bars, {0: Signal("buy", 5.0, None)}, cal_with(release))
    (t,) = r.trades
    # bar 5 closes at 10:30 = release - 15 min -> exit at that close; that time is inside the window -> slip x3
    assert t.exit_reason == "news_exit" and t.exit_time == T0 + 6 * M5
    assert t.exit_price == pytest.approx(2400.0 - 0.10 * 3)
    r_off = run_news(bars, {0: Signal("buy", 5.0, None)}, cal_with(release), rules=NewsRules(flat_before=False))
    assert r_off.trades[0].exit_reason == "end_of_data"


def test_news_rules_validation():
    with pytest.raises(ValueError):
        NewsRules(slippage_multiplier=0.5)
    with pytest.raises(ValueError, match="both"):
        run_backtest(flat_bars(3), Scripted({}), spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                     config=EngineConfig(initial_equity=1e5), events_calendar=cal_with(T0))


def test_fx_series_used_for_sizing_and_conversion_point_in_time():
    bars = base_bars() + [bar(T0 + 2 * M5, 2401.0, 2403.41, 2400.9, 2403.0)]
    # rate 10 available before the entry; rate 20 published between entry (10:05) and exit (10:15)
    fx = FxSeries("USDZAR", [T0 - 86_400_000, T0 + 2 * M5 + MIN], [10.0, 20.0])
    r = run_backtest(bars, Scripted(BUY), spec=SPEC, limits=LIMITS, costs=COSTS, calendar=XauCfd(),
                     config=EngineConfig(initial_equity=100_000.0), fx_series=fx)
    (t,) = r.trades
    assert t.fx_entry == 10.0 and t.lots == pytest.approx(0.40)      # sized with the rate known at entry
    assert t.fx_exit == 20.0 and t.net_account == pytest.approx(116.0 * 20)  # converted at the exit-time rate
    assert r.fx_source.startswith("ECB")


def test_missing_fx_rate_blocks_entry():
    fx = FxSeries("USDZAR", [T0 + 10 * M5], [10.0])  # nothing published before the fill
    r = run_backtest(base_bars() + flat_bars(2, start=T0 + 2 * M5), Scripted(BUY), spec=SPEC, limits=LIMITS,
                     costs=COSTS, calendar=XauCfd(), config=EngineConfig(initial_equity=1e5), fx_series=fx)
    assert r.trades == [] and [e.kind for e in r.events if e.kind == "blocked_fx"] == ["blocked_fx"]
