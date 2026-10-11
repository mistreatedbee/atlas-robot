from __future__ import annotations

from atlas.ingest import ingest
from atlas.monitoring import check_health
from atlas.risk.gate import KillSwitch

from .conftest import CRYPTO, H1, FakeProvider, FixedClock, utc_ms

T0 = utc_ms(2026, 10, 1)


def run(store, tmp_path, **kw):
    return check_health(store, now_ms=kw.pop("now", T0 + 48 * H1), kill_switch=KillSwitch(tmp_path / "KS"),
                        calendars={"BTCUSDT": "crypto_24x7"}, **kw)


def test_empty_database_warns(store, tmp_path):
    rep = run(store, tmp_path)
    assert rep.level == "warn" and any(f.check == "data" for f in rep.findings)


def test_healthy_series(store, tmp_path):
    ingest(store, FakeProvider(), CRYPTO, "H1", T0, T0 + 48 * H1, clock=FixedClock(T0 + 48 * H1))
    rep = run(store, tmp_path, max_data_age_minutes=90)
    assert rep.level == "ok" and rep.exit_code == 0


def test_stale_data_is_critical(store, tmp_path):
    ingest(store, FakeProvider(), CRYPTO, "H1", T0, T0 + 24 * H1, clock=FixedClock(T0 + 48 * H1))
    rep = run(store, tmp_path, max_data_age_minutes=90)
    assert rep.level == "critical" and any(f.check == "freshness" and f.level == "critical" for f in rep.findings)


def test_failed_ingestion_and_kill_switch_are_critical(store, tmp_path):
    ingest(store, FakeProvider(fail_on_call=2), CRYPTO, "H1", T0, T0 + 48 * H1, clock=FixedClock(T0 + 48 * H1))
    rep = run(store, tmp_path)
    assert rep.exit_code == 2 and any(f.check == "ingestion" and f.level == "critical" for f in rep.findings)
    KillSwitch(tmp_path / "KS").engage("test")
    assert any(f.check == "kill_switch" and f.level == "critical" for f in run(store, tmp_path).findings)


def test_rejects_and_conflicts_warn(store, tmp_path):
    ingest(store, FakeProvider(rejects_per_call=1), CRYPTO, "H1", T0, T0 + 24 * H1, clock=FixedClock(T0 + 48 * H1))
    ingest(store, FakeProvider(price_shift=1.0), CRYPTO, "H1", T0, T0 + 24 * H1, clock=FixedClock(T0 + 48 * H1))
    checks = {(f.check, f.level) for f in run(store, tmp_path).findings}
    assert ("conflicts", "warn") in checks
