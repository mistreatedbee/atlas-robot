"""Performance metrics, net of all costs (docs/13 R8)."""

from __future__ import annotations

import math
import statistics
from collections import Counter

from ..sessions import trading_day
from .engine import BacktestResult

MIN_DAYS_FOR_SHARPE = 60


def _drawdown(curve: list[tuple[int, float]], daily_labels: list[str]) -> dict:
    day_index = {d: k for k, d in enumerate(daily_labels)}
    peak = -math.inf
    peak_day = None
    max_depth, max_pct, max_duration_days = 0.0, 0.0, 0
    unrecovered = False
    for t, eq in curve:
        if eq >= peak:
            peak, peak_day = eq, trading_day(t - 1)  # t is a bar close; label by the bar itself
            unrecovered = False
            continue
        depth = peak - eq
        if depth > max_depth:
            max_depth, max_pct = depth, depth / peak if peak > 0 else math.inf
        unrecovered = True
        if peak_day is not None:
            duration = day_index[trading_day(t - 1)] - day_index[peak_day]
            max_duration_days = max(max_duration_days, duration)
    return {"max_drawdown": max_depth, "max_drawdown_pct": max_pct,
            "max_drawdown_duration_days": max_duration_days, "drawdown_unrecovered_at_end": unrecovered}


def compute_metrics(result: BacktestResult) -> dict:
    trades = result.trades
    nets = [t.net_account for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(x for x in losses if x < 0)
    final = result.equity_curve[-1][1] if result.equity_curve else result.initial_equity

    labels = list(result.daily)
    m = {
        "initial_equity": result.initial_equity,
        "final_equity": final,
        "net_pnl": final - result.initial_equity,
        "net_pnl_usd": sum(t.net_usd for t in trades),
        "return_pct": (final - result.initial_equity) / result.initial_equity,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_win": statistics.fmean(wins) if wins else None,
        "avg_loss": statistics.fmean(losses) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else ("undefined (0 losses)" if trades else None),
        "expectancy": statistics.fmean(nets) if nets else None,
        "expectancy_r": statistics.fmean(t.r_multiple for t in trades) if trades else None,
        "worst_r": min((t.r_multiple for t in trades), default=None),
        "total_costs_usd": sum(t.spread_cost_usd + t.slippage_cost_usd + t.commission_usd for t in trades),
        "exposure": result.bars_in_position / result.bars_total if result.bars_total else 0.0,
        "trading_days": len(labels),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "held_through_gap": sum(t.held_through_gap for t in trades),
        "events": dict(Counter(e.kind for e in result.events)),
        "expired_wait_by_hour": dict(Counter(e.detail for e in result.events if e.kind == "expired_wait")),
        "stopped_early": result.stopped_early,
        "fx_source": result.fx_source,
    }
    priced = [t for t in trades if t.r_unit > 0]
    m["costs_per_trade_r"] = statistics.fmean(
        (t.spread_cost_usd + t.slippage_cost_usd + t.commission_usd) * t.fx_entry / t.r_unit
        for t in priced) if priced else None
    m.update(_drawdown(result.equity_curve, labels))

    # Daily returns over ALL trading days (flat days = 0), R8.
    rets = [(end - start) / start for start, end in result.daily.values() if start > 0]
    if len(rets) >= MIN_DAYS_FOR_SHARPE and statistics.pstdev(rets) > 0:
        mu, sd = statistics.fmean(rets), statistics.pstdev(rets)
        sharpe = mu / sd * math.sqrt(252)
        daily_sr = mu / sd
        se = math.sqrt((1 + 0.5 * daily_sr ** 2) / len(rets)) * math.sqrt(252)
        downside = [min(r, 0.0) for r in rets]
        dd = math.sqrt(statistics.fmean(x * x for x in downside))
        m["sharpe"] = sharpe
        m["sharpe_ci95"] = [sharpe - 1.96 * se, sharpe + 1.96 * se]
        m["sortino"] = mu / dd * math.sqrt(252) if dd > 0 else None
    else:
        m["sharpe"] = m["sortino"] = None
        m["sharpe_note"] = f"not reported: {len(rets)} trading days (< {MIN_DAYS_FOR_SHARPE}) or zero variance"
    return m
