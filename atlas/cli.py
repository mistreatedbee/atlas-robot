"""Command-line interface: `python -m atlas <command>`. Research data only; no trading commands exist."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone

from .config import ConfigError, load_settings
from .ingest import ingest, system_clock_ms, validate_range
from .models import DataLabel
from .logging_setup import setup_logging
from .providers import build_provider
from .storage import Store

log = logging.getLogger("atlas.cli")

SAMPLE_BANNER = "*** SYNTHETIC SAMPLE DATA: generated prices, NOT real market data. Do not use for research conclusions. ***"


def parse_utc(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # CLI dates without an offset are UTC by definition
    return int(dt.timestamp() * 1000)


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_report(summary: dict) -> None:
    print(f"  quality status        : {summary['status']}")
    print(f"  candles checked       : {summary['candle_count']} "
          f"({fmt_ms(summary['first_open_time'])} -> {fmt_ms(summary['last_close_time'])})")
    print(f"  invalid (excluded)    : {summary.get('invalid_candles_excluded', 0)}")
    print(f"  expected closures     : {summary['expected_closure_count']}")
    print(f"  unexpected gaps       : {summary['unexpected_gap_count']} "
          f"({summary['unexpected_missing_bars']} missing bars)")
    for g in summary["gaps"]:
        if not g["expected_closure"]:
            print(f"    gap {fmt_ms(g['start'])} -> {fmt_ms(g['end'])} ({g['missing_bars']} bars)")
    print(f"  series issues         : {summary['issue_counts'] or 'none'}")


def cmd_instruments(settings, args) -> int:
    for inst in settings.instruments.values():
        flag = "enabled " if inst.enabled else "disabled"
        symbols = ", ".join(f"{p}={s}" for p, s in inst.symbols.items())
        print(f"{inst.instrument_id:<8} {inst.asset_class:<12} {flag} {inst.session_calendar:<12} {symbols}")
    return 0


def cmd_ingest(settings, args) -> int:
    instrument = settings.instrument(args.instrument)
    provider = build_provider(settings.provider(args.provider), settings.root)
    if provider.data_label is DataLabel.SYNTHETIC_SAMPLE:
        print(SAMPLE_BANNER, file=sys.stderr)
    end = parse_utc(args.end) if args.end else system_clock_ms()
    start = None if args.incremental else parse_utc(args.start)
    with Store(settings.database_path) as store:
        store.upsert_instruments(settings.instruments.values(), system_clock_ms())
        result = ingest(store, provider, instrument, args.timeframe, start, end)
        run = result.run
        print(f"run {result.run_id}: {result.status}"
              + (f" (resumed from {result.resumed_from})" if result.resumed_from else ""))
        print(f"  window                : {fmt_ms(result.start_ms)} -> {fmt_ms(result.end_ms)}")
        print(f"  received/inserted     : {run['received']}/{run['inserted']}")
        print(f"  duplicates/conflicts  : {run['duplicates']}/{run['conflicts']}")
        print(f"  rejected/invalid      : {run['rejected']}/{run['invalid']}")
        print(f"  unfinished skipped    : {run['unfinished_skipped']}")
        if result.error:
            print(f"  error                 : {result.error}")
            print("  rerun the same command to resume from the last committed checkpoint")
            return 1
        _print_report(result.report.summary() | {"invalid_candles_excluded": run["invalid"]})
    return 0


def cmd_validate(settings, args) -> int:
    instrument = settings.instrument(args.instrument)
    provider_cfg = settings.provider(args.provider)
    provider = build_provider(provider_cfg, settings.root)
    with Store(settings.database_path) as store:
        start = parse_utc(args.start) if args.start else 0
        end = parse_utc(args.end) if args.end else system_clock_ms()
        staleness = args.max_staleness_minutes * 60_000 if args.max_staleness_minutes else None
        report = validate_range(store, instrument, provider.name, provider.venue, args.timeframe, start, end,
                                max_staleness_ms=staleness)
        summary = report.summary()
        summary["invalid_candles_excluded"] = store.count(
            "candles", "instrument_id=? AND provider=? AND timeframe=? AND validation_status='invalid' "
            "AND open_time_utc>=? AND open_time_utc<?", (instrument.instrument_id, provider.name, args.timeframe,
                                                          start, end))
        print(f"{instrument.instrument_id} {args.timeframe} from {provider.name} "
              f"(labels: {sorted(store.data_labels(instrument.instrument_id, provider.name, args.timeframe))})")
        _print_report(summary)
        if args.json:
            print(json.dumps(summary, indent=2))
    return 0 if summary["status"] != "invalid" else 2


def cmd_show(settings, args) -> int:
    with Store(settings.database_path) as store:
        start = parse_utc(args.start) if args.start else None
        end = parse_utc(args.end) if args.end else None
        candles = store.get_candles(args.instrument, args.provider, args.timeframe, start_ms=start, end_ms=end,
                                    include_invalid=args.include_invalid)
        labels = store.data_labels(args.instrument, args.provider, args.timeframe)
        if DataLabel.SYNTHETIC_SAMPLE.value in labels:
            print(SAMPLE_BANNER, file=sys.stderr)
        rows = candles[-args.limit:] if args.limit else candles
        writer = csv.writer(sys.stdout)
        writer.writerow(["open_time_utc", "open", "high", "low", "close", "volume", "volume_kind", "price_basis"])
        for c in rows:
            writer.writerow([fmt_ms(c.open_time), c.open, c.high, c.low, c.close, c.volume,
                             c.volume_kind.value, c.price_basis.value])
        print(f"# {len(rows)} of {len(candles)} candles; data_label={sorted(labels)}", file=sys.stderr)
    return 0


def cmd_runs(settings, args) -> int:
    with Store(settings.database_path) as store:
        for r in store.list_runs(args.limit):
            print(f"{r['run_id'][:8]} {r['status']:<9} {r['instrument_id']:<8} {r['timeframe']:<4} "
                  f"{r['provider']:<17} {fmt_ms(r['requested_start_utc'])}->{fmt_ms(r['requested_end_utc'])} "
                  f"ins={r['inserted']} dup={r['duplicates']} rej={r['rejected']} inv={r['invalid']} "
                  f"ckpt={fmt_ms(r['checkpoint_utc'])}"
                  + (f" resumed_from={r['resumed_from_run_id'][:8]}" if r["resumed_from_run_id"] else "")
                  + (f" error={r['error']}" if r["error"] else ""))
    return 0


def cmd_risk_feasibility(settings, args) -> int:
    from .brokers.spec import SpecError, load_spec
    from .risk.gate import CostInputs, RiskLimits, feasibility

    spec = load_spec(settings.root / args.spec)
    limits = RiskLimits.load(settings.root / args.limits)
    costs = CostInputs(spread_usd=args.spread_usd, slippage_usd=args.slippage_usd,
                       usd_to_account=args.usd_to_account, sources=tuple(args.source))
    try:
        rows = feasibility(spec, args.equity, [float(x) for x in args.stops.split(",")], costs, limits)
    except SpecError as exc:
        print(f"cannot assess: {exc}", file=sys.stderr)
        return 2
    print(f"spec: {spec.label}")
    if spec.status.value != "verified":
        print("*** NOT VERIFIED BROKER VALUES: results illustrate the method only. ***")
    print(f"equity {args.equity:.2f} | spread {args.spread_usd} + slippage {args.slippage_usd} USD/oz | "
          f"USD->account {args.usd_to_account} | per-trade limit {limits.max_risk_per_trade_fraction:.2%}")
    print(f"sources: {'; '.join(costs.sources)}")
    print(f"{'stop USD/oz':>11} {'risk @ min vol':>15} {'% equity':>9}  permitted")
    for r in rows:
        print(f"{r.stop_usd:>11.2f} {r.risk_at_min_volume:>15.2f} {r.risk_fraction_at_min_volume:>9.2%}  "
              f"{'yes' if r.permitted else 'NO'}")
    return 0


def _print_metrics(m: dict) -> None:
    keys = ["trades", "net_pnl", "net_pnl_usd", "return_pct", "win_rate", "avg_win", "avg_loss", "profit_factor",
            "expectancy", "expectancy_r", "worst_r", "costs_per_trade_r", "max_drawdown", "max_drawdown_pct",
            "max_drawdown_duration_days", "exposure", "sharpe", "sharpe_ci95", "sortino", "trading_days"]
    for k in keys:
        v = m.get(k)
        if isinstance(v, float):
            v = f"{v:.4%}" if k in ("return_pct", "win_rate", "max_drawdown_pct", "exposure") else f"{v:,.4f}"
        print(f"  {k:<28}: {v}")
    if m.get("sharpe_note"):
        print(f"  {'sharpe_note':<28}: {m['sharpe_note']}")
    print(f"  {'exit_reasons':<28}: {m.get('exit_reasons')}")
    print(f"  {'events':<28}: {m.get('events')}")


def cmd_backtest(settings, args) -> int:
    from .backtest.runner import ExperimentError, load_experiment_recorded, run_experiment
    from .research.ledger import LedgerError

    with Store(settings.database_path) as store:
        try:
            exp = load_experiment_recorded(store, settings.root / args.experiment, settings.root, args.period)
            exp_id, m = run_experiment(store, exp, args.period, cost_multiplier=args.cost_multiplier,
                                       confirm_test=args.confirm_test, allow_repeat_test=args.allow_repeat_test)
        except (ExperimentError, LedgerError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    m.pop("_result", None)
    if "synthetic_sample" in m.get("data_labels", []):
        print(SAMPLE_BANNER, file=sys.stderr)
    print(f"experiment {exp_id}: {exp.name} [{args.period}] cost x{args.cost_multiplier} ({m['cost_status']})")
    _print_metrics(m)
    return 0


def cmd_experiments(settings, args) -> int:
    from .research.ledger import Ledger

    with Store(settings.database_path) as store:
        for r in Ledger(store).list(args.limit):
            m = json.loads(r["metrics_json"]) if r["metrics_json"] else {}
            print(f"{r['experiment_id'][:8]} {r['status']:<9} {r['period']:<10} {r['strategy']:<13} "
                  f"trades={m.get('trades', '-')} net={m.get('net_pnl', '-')} cost={r['cost_status']} "
                  f"data={r['data_label']} code={r['code_version']}" + (f" error={r['error']}" if r["error"] else ""))
    return 0


def cmd_random_baseline(settings, args) -> int:
    from .backtest.runner import load_experiment, matched_random_percentile

    exp = load_experiment(settings.root / args.experiment, settings.root)
    with Store(settings.database_path) as store:
        out = matched_random_percentile(store, exp, args.period, seeds=args.seeds)
    out.pop("random_net_pnls", None)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_health(settings, args) -> int:
    from .monitoring import check_health
    from .risk.gate import KillSwitch

    with Store(settings.database_path) as store:
        rep = check_health(store, now_ms=system_clock_ms(), kill_switch=KillSwitch(settings.root / args.kill_switch),
                           calendars={i.instrument_id: i.session_calendar for i in settings.instruments.values()},
                           max_data_age_minutes=args.max_data_age_minutes)
    from .monitoring import check_heartbeat
    check_heartbeat(rep, settings.root / "logs" / "heartbeat.json", system_clock_ms(),
                    max_age_minutes=args.session_minutes_max)
    for f in rep.findings:
        print(f"{f.level.upper():<8} {f.check:<12} {f.detail}")
    print(f"overall: {rep.level}")
    return rep.exit_code


def cmd_fx_update(settings, args) -> int:
    from datetime import date, datetime, timezone

    from .fx import ECB_HOST, fetch_usdzar, store_rates
    from .http import ReadOnlyHttpClient

    http = ReadOnlyHttpClient({ECB_HOST}, min_interval_s=1.0)
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    rows = fetch_usdzar(http, date.fromisoformat(args.start), end)
    with Store(settings.database_path) as store:
        n = store_rates(store, "USDZAR", rows, system_clock_ms())
    print(f"USDZAR: fetched {len(rows)} ECB reference days, inserted {n} new"
          + (f"; latest {rows[-1][0]} = {rows[-1][1]:.4f}" if rows else ""))
    return 0


def cmd_mt5_check(settings, args) -> int:
    """Read-only check against the local MT5 terminal (Windows). Never trades, never handles credentials."""
    from .broker.mt5_readonly import BrokerReadError, Mt5ReadOnly, ReadOnlyViolation, spec_toml_from_snapshots

    try:
        ro = Mt5ReadOnly()
        acc = ro.connect()
    except (BrokerReadError, ReadOnlyViolation) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        masked = f"***{str(acc.login)[-3:]}"
        print(f"account   : {masked} on {acc.server} ({acc.company}); mode={acc.trade_mode}; "
              f"trading allowed in this session={acc.trade_allowed}")
        print(f"balance   : {acc.balance:.2f} {acc.currency}; equity {acc.equity:.2f}; free margin {acc.margin_free:.2f}; "
              f"leverage 1:{acc.leverage:g}; margin call {acc.margin_so_call:g}%; stop-out {acc.margin_so_so:g}%")
        sym = ro.symbol_snapshot(args.symbol)
        print(f"symbol    : {sym.name}: contract {sym.contract_size:g}, digits {sym.digits}, tick {sym.tick_size:g} "
              f"(value {sym.tick_value:g} {acc.currency}), volume {sym.volume_min:g}..{sym.volume_max:g} step "
              f"{sym.volume_step:g}, stops level {sym.stops_level_points} pts, freeze {sym.freeze_level_points} pts")
        print(f"            swap long {sym.swap_long:g} / short {sym.swap_short:g} (triple day {sym.swap_rollover3days}), "
              f"spread {sym.spread_points} pts ({'floating' if sym.spread_float else 'fixed'})")
        t = ro.tick(args.symbol)
        print(f"quote     : bid {t.bid} ask {t.ask} spread {t.spread:.2f} (server time {t.time_server_s})")
        pos = ro.positions(args.symbol)
        print(f"positions : {len(pos)} open on {args.symbol}")
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from .broker.mt5_readonly import commission_from_deals
        now_dt = _dt.now(_tz.utc)
        com = commission_from_deals(ro.deals(now_dt - _td(days=90), now_dt), args.symbol, acc.currency)
        print(f"commission: {com.per_lot_round_turn if com.per_lot_round_turn is not None else 'unknown'} "
              f"{com.currency} per lot round turn ({com.note}; {com.deals_used} deals, last 90 days)")
        if args.write_spec:
            out = settings.root / args.write_spec
            out.write_text(spec_toml_from_snapshots(sym, acc, broker="Vault Markets",
                                                    server_time_model=args.server_time_model,
                                                    source_note="atlas mt5-check"), encoding="utf-8")
            print(f"spec      : written to {out} (add commission before it will load; see docs/14)")
    finally:
        ro.close()
    return 0


def cmd_paper_run(settings, args) -> int:
    """PAPER trading on live MT5 quotes (Windows). Reads only; simulated fills only; never places real orders."""
    import time as _t

    from .backtest.runner import build_strategy, load_experiment
    from .broker.live_paper import LivePaperDriver
    from .broker.mt5_readonly import BrokerReadError, Mt5ReadOnly, ReadOnlyViolation
    from .broker.paper import ConnectionMonitor, PaperBroker
    from .broker.session import PaperSession
    from .brokers.server_time import ServerTimeError, ServerTimeModel
    from .brokers.spec import load_spec
    from .risk.gate import KillSwitch, RiskLimits
    from .sessions import XauCfd

    exp = load_experiment(settings.root / args.experiment, settings.root)
    spec = load_spec(settings.root / args.spec) if args.spec else load_spec(exp.spec_path)
    try:
        ro = Mt5ReadOnly()
        acc = ro.connect()
    except (BrokerReadError, ReadOnlyViolation) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    equity = acc.equity if args.equity_from_account else exp.engine.initial_equity
    print(f"PAPER TRADING (simulated fills; no real orders) on {acc.server}, mode={acc.trade_mode}; "
          f"spec {spec.label}; paper equity {equity:.2f}")
    with Store(settings.database_path) as store:
        broker = PaperBroker(store, spec, kill_switch=KillSwitch(settings.root / args.kill_switch),
                             slippage_usd=exp.costs.slippage_usd)
        strategy = build_strategy(exp.strategy_name, exp.strategy_params)
        session = PaperSession(strategy, broker=broker, spec=spec, limits=RiskLimits.load(exp.limits_path),
                               costs=exp.costs, calendar=XauCfd(), initial_equity=equity,
                               strategy_id=f"{exp.hypothesis_id}:{strategy.name}", candles=[])
        driver = LivePaperDriver(ro, session, symbol=args.symbol, timeframe=exp.data["timeframe"],
                                 server_time=ServerTimeModel(args.server_time_model),
                                 monitor=ConnectionMonitor(), calendar=XauCfd())
        try:
            driver.run(until_ms=int(_t.time() * 1000) + int(args.minutes * 60_000), poll_ms=int(args.poll_seconds * 1000))
        except ServerTimeError as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("interrupted: open paper positions stay journalled and are managed again on the next run")
        finally:
            ro.close()
        rep = session.rep
        print(f"bars closed {len(session.candles)}, paper entries {len(rep.entries)}, exits {len(rep.exits)}, "
              f"open now {len(broker.open_positions(args.symbol))}, clock verified {driver.clock_verified}")
        for t, kind, detail in (driver.log + rep.events)[-10:]:
            print(f"  {fmt_ms(t)} {kind} {detail}")
    return 0


def cmd_import_ticks(settings, args) -> int:
    from .ticks import TickImportError, import_ticks

    cfg = settings.provider(args.provider)
    model = cfg.options.get("server_time_model")
    if cfg.kind != "mt5_export" or not model:
        print(f"error: provider {args.provider} is not an MT5 export provider with a server_time_model", file=sys.stderr)
        return 2
    with Store(settings.database_path) as store:
        try:
            r = import_ticks(store, settings.root / args.file, instrument_id=args.instrument, provider=cfg.name,
                             venue=cfg.options["venue"], server_time_model=model, now_ms=system_clock_ms())
        except TickImportError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    if r.already_imported:
        print(f"already imported (import {r.import_id[:8]}); nothing written")
    else:
        print(f"imported {r.rows_imported} ticks ({r.rows_rejected} rejected, {r.duplicates_ignored} duplicates); "
              f"server time verified by bar run {r.basis_run_id[:8]}")
    return 0


def cmd_spread_profile(settings, args) -> int:
    from .events import EventCalendar
    from .sessions import XauCfd
    from .ticks import load_ticks, spread_profile, suggested_costs

    cfg = settings.provider(args.provider)
    with Store(settings.database_path) as store:
        ticks = load_ticks(store, args.instrument, cfg.name, cfg.options["venue"], parse_utc(args.start), parse_utc(args.end))
    if not ticks:
        print("no ticks in range; run `atlas import-ticks` first", file=sys.stderr)
        return 2
    cal = EventCalendar.load(settings.root / "config/events/us_tier1.csv")
    prof = spread_profile(ticks, calendar=XauCfd(), reopen_minutes=args.reopen_minutes, events=cal)
    print(f"time-weighted spread, USD/oz ({len(ticks)} quotes, {fmt_ms(ticks[0].time_ms)} -> {fmt_ms(ticks[-1].time_ms)})")
    print(f"{'bucket':<16}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>8}{'minutes':>10}")
    for k, v in prof.items():
        print(f"{k:<16}{v.get('p50', 0):>8.2f}{v.get('p90', 0):>8.2f}{v.get('p99', 0):>8.2f}{v['max']:>8.2f}"
              f"{v['minutes']:>10.1f}")
    if "normal" in prof:
        sug = suggested_costs(prof, tick_size=args.tick_size, source=f"{cfg.name} ticks {args.start}..{args.end}")
        print(f"suggested [costs]: base_spread_usd = {sug['base_spread_usd']}, reopen_spread_usd = "
              f"{sug['reopen_spread_usd']} ({sug['basis']}); {sug['note']}")
    return 0


def cmd_supervise(settings, args) -> int:
    """Run `paper-run` sessions back to back with crash restarts, heartbeat and a stop file (paper only)."""
    from .supervisor import Supervisor

    def job() -> int:
        return main(["--config", str(args.config), "paper-run", "--experiment", args.experiment,
                     "--minutes", str(args.session_minutes), "--server-time-model", args.server_time_model]
                    + (["--equity-from-account"] if args.equity_from_account else []))

    sup = Supervisor(job, heartbeat=settings.root / "logs" / "heartbeat.json",
                     alerts=settings.root / "logs" / "alerts.jsonl", stop_file=settings.root / args.stop_file,
                     max_consecutive_crashes=args.max_crashes)
    res = sup.run()
    print(f"supervisor: {res.runs} runs, {res.crashes} crashes, stopped by {res.stopped_by}")
    return 0 if res.stopped_by != "max_crashes" else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas", description="Project Atlas research data tools (no trading).")
    p.add_argument("--config", default="atlas.toml", help="path to atlas.toml (default: ./atlas.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("instruments", help="list the configured instrument registry")

    ing = sub.add_parser("ingest", help="ingest historical candles")
    ing.add_argument("--provider", required=True)
    ing.add_argument("--instrument", required=True)
    ing.add_argument("--timeframe", required=True)
    group = ing.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", help="UTC start, ISO-8601 (e.g. 2026-10-01 or 2026-10-01T00:00:00Z)")
    group.add_argument("--incremental", action="store_true", help="continue after the latest stored candle")
    ing.add_argument("--end", help="UTC end (exclusive); default now. Unclosed bars are never ingested.")

    val = sub.add_parser("validate", help="run series validation over stored candles and save a quality report")
    for a in ("--provider", "--instrument", "--timeframe"):
        val.add_argument(a, required=True)
    val.add_argument("--start")
    val.add_argument("--end")
    val.add_argument("--max-staleness-minutes", type=float)
    val.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="print stored candles as CSV")
    for a in ("--provider", "--instrument", "--timeframe"):
        show.add_argument(a, required=True)
    show.add_argument("--start")
    show.add_argument("--end")
    show.add_argument("--limit", type=int, default=10, help="last N candles (0 = all)")
    show.add_argument("--include-invalid", action="store_true")

    rf = sub.add_parser("risk-feasibility", help="risk of the smallest possible trade at given stop distances")
    rf.add_argument("--spec", required=True, help="broker spec TOML (e.g. config/brokers/PROVISIONAL_dev_xauusd.toml)")
    rf.add_argument("--limits", default="config/risk.toml")
    rf.add_argument("--equity", type=float, required=True, help="account equity in account currency")
    rf.add_argument("--usd-to-account", type=float, required=True, help="USD -> account currency rate (no default)")
    rf.add_argument("--spread-usd", type=float, required=True)
    rf.add_argument("--slippage-usd", type=float, required=True)
    rf.add_argument("--source", action="append", required=True, help="where each cost/FX input came from (repeatable)")
    rf.add_argument("--stops", default="0.5,1,2,3,5", help="comma-separated stop distances in USD/oz")

    bt = sub.add_parser("backtest", help="run an experiment config on stored data and record it in the ledger")
    bt.add_argument("--experiment", required=True, help="experiment TOML (see experiments/)")
    bt.add_argument("--period", required=True, choices=["train", "validation", "test"])
    bt.add_argument("--cost-multiplier", type=float, default=1.0, help="stress: scale spread and slippage (>= 1)")
    bt.add_argument("--confirm-test", action="store_true", help="required for the single test-period look")
    bt.add_argument("--allow-repeat-test", action="store_true", help="override (recorded) for a repeated test look")

    ex = sub.add_parser("experiments", help="list the experiment ledger")
    ex.add_argument("--limit", type=int, default=20)

    rb = sub.add_parser("random-baseline", help="rank a strategy against matched random entries (R9.2)")
    rb.add_argument("--experiment", required=True)
    rb.add_argument("--period", required=True, choices=["train", "validation"])
    rb.add_argument("--seeds", type=int, default=1000)

    hl = sub.add_parser("health", help="operational health check (exit 0 ok, 1 warn, 2 critical)")
    hl.add_argument("--max-data-age-minutes", type=float, help="staleness threshold in trading minutes (real data only)")
    hl.add_argument("--kill-switch", default="data/KILL_SWITCH")
    hl.add_argument("--session-minutes-max", type=float, default=300,
                    help="heartbeat older than this while 'running' is critical (sessions write one per start)")

    fx = sub.add_parser("fx-update", help="fetch daily USD/ZAR from ECB reference rates (no credentials)")
    fx.add_argument("--start", required=True)
    fx.add_argument("--end")

    mc = sub.add_parser("mt5-check", help="read-only check of the local MT5 terminal (Windows; never trades)")
    mc.add_argument("--symbol", default="XAUUSD")
    mc.add_argument("--write-spec", help="write a verified spec file from the terminal, e.g. config/brokers/vault_live.toml")
    mc.add_argument("--server-time-model", default="ny_close_midnight",
                    help="must already have passed the export check (docs/10); recorded in the spec")

    pr = sub.add_parser("paper-run", help="PAPER trade on live MT5 quotes (Windows; simulated fills only)")
    pr.add_argument("--experiment", required=True, help="strategy, costs, limits and spec (experiments/*.toml)")
    pr.add_argument("--spec", help="override spec file, e.g. one written by `atlas mt5-check --write-spec`")
    pr.add_argument("--symbol", default="XAUUSD")
    pr.add_argument("--server-time-model", default="ny_close_midnight")
    pr.add_argument("--minutes", type=float, default=60.0)
    pr.add_argument("--poll-seconds", type=float, default=1.0)
    pr.add_argument("--equity-from-account", action="store_true", help="paper equity = live account equity")
    pr.add_argument("--kill-switch", default="data/KILL_SWITCH")

    it = sub.add_parser("import-ticks", help="import an MT5 tick export (bid/ask) after a verified bar import")
    it.add_argument("--provider", default="vault_mt5")
    it.add_argument("--instrument", default="XAUUSD")
    it.add_argument("--file", required=True, help="e.g. data/imports/vault_markets/XAUUSD_ticks_20261005_20261009.csv")

    sp = sub.add_parser("spread-profile", help="measured, time-weighted spread profile from stored ticks")
    sp.add_argument("--provider", default="vault_mt5")
    sp.add_argument("--instrument", default="XAUUSD")
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--reopen-minutes", type=int, default=15)
    sp.add_argument("--tick-size", type=float, default=0.01, help="from the verified spec")

    sv = sub.add_parser("supervise", help="unattended PAPER trading: restarts, heartbeat, stop file (Windows)")
    sv.add_argument("--experiment", required=True)
    sv.add_argument("--session-minutes", type=float, default=240)
    sv.add_argument("--server-time-model", default="ny_close_midnight")
    sv.add_argument("--equity-from-account", action="store_true")
    sv.add_argument("--stop-file", default="data/STOP_ATLAS")
    sv.add_argument("--max-crashes", type=int, default=5)

    runs = sub.add_parser("runs", help="list ingestion runs")
    runs.add_argument("--limit", type=int, default=20)
    return p


COMMANDS = {"instruments": cmd_instruments, "ingest": cmd_ingest, "validate": cmd_validate,
            "show": cmd_show, "runs": cmd_runs, "risk-feasibility": cmd_risk_feasibility,
            "backtest": cmd_backtest, "health": cmd_health, "fx-update": cmd_fx_update, "mt5-check": cmd_mt5_check,
            "paper-run": cmd_paper_run, "import-ticks": cmd_import_ticks, "spread-profile": cmd_spread_profile,
            "supervise": cmd_supervise, "experiments": cmd_experiments, "random-baseline": cmd_random_baseline}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_path, logging.DEBUG if args.verbose else logging.INFO)
    try:
        return COMMANDS[args.command](settings, args)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
