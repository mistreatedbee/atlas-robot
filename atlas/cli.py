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
    from .backtest.runner import ExperimentError, load_experiment, run_experiment
    from .research.ledger import LedgerError

    exp = load_experiment(settings.root / args.experiment, settings.root)
    with Store(settings.database_path) as store:
        try:
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

    fx = sub.add_parser("fx-update", help="fetch daily USD/ZAR from ECB reference rates (no credentials)")
    fx.add_argument("--start", required=True)
    fx.add_argument("--end")

    runs = sub.add_parser("runs", help="list ingestion runs")
    runs.add_argument("--limit", type=int, default=20)
    return p


COMMANDS = {"instruments": cmd_instruments, "ingest": cmd_ingest, "validate": cmd_validate,
            "show": cmd_show, "runs": cmd_runs, "risk-feasibility": cmd_risk_feasibility,
            "backtest": cmd_backtest, "health": cmd_health, "fx-update": cmd_fx_update, "experiments": cmd_experiments, "random-baseline": cmd_random_baseline}


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
