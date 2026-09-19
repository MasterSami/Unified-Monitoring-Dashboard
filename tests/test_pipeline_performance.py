"""Pipeline performance + concurrency behaviour on a large real estate.

Pins the fixes from the full review of the collect -> topology -> correlate
-> incidents pipeline: network I/O never happens under the write lock,
bulk writes are serialized, a manual sweep never doubles the automatic one,
history backfills are incremental and skip unchanged rows, the correlation
batch walks EVERY open event in rounds, and a fresh database gets default
rules so the Incidents tab fills without anyone calling the rule API.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.collectors.base import BaseCollector
from app.collectors.zabbix import ZabbixCollector
from app.config import get_settings
from app.correlation_engine import run_correlation_batch
from app.correlation_rules import DEFAULT_RULES, ensure_default_rules, validate_rule_conditions
from app.db import Base, SessionLocal, write_lock
from app.models import (
    Alert,
    CanonicalEntity,
    CorrelationRule,
    EntityType,
    HostStatus,
    LogicalEvent,
    LogicalEventStatus,
    SourcePlatform,
)
from app.normalizer import upsert_alerts, upsert_resolved_alerts
from app.servers import ServerConfig

_counter = itertools.count()


def _tag(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# --- default rules -----------------------------------------------------------


def test_default_rules_seed_once_on_an_empty_database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        assert ensure_default_rules(db) == len(DEFAULT_RULES)
        db.commit()
        assert ensure_default_rules(db) == 0  # never re-seeded over existing rules
        stored = {r.rule_id for r in db.scalars(select(CorrelationRule)).all()}
        assert stored == {spec["rule_id"] for spec in DEFAULT_RULES}
        for spec in DEFAULT_RULES:
            validate_rule_conditions(spec["conditions"])  # each is a legal, strong rule
    finally:
        db.close()


# --- collector run phases ----------------------------------------------------


class _PhaseCollector(BaseCollector):
    name = "zabbix"
    platform = SourcePlatform.zabbix

    def __init__(self, config: ServerConfig, settings) -> None:
        super().__init__(config, settings)
        self.lock_during_fetch: bool | None = None
        self.history_since: list[datetime | None] = []

    def collect_hosts(self) -> list[dict]:
        self.lock_during_fetch = write_lock.locked()
        return [{"external_id": "h1", "hostname": _tag("phase-host"), "ip": "10.99.0.1", "status": HostStatus.up}]

    def collect_alerts(self) -> list[dict]:
        return []

    def collect_resolved_alerts(self, since=None) -> list[dict]:
        self.history_since.append(since)
        return []


def test_collector_fetches_outside_the_lock_and_writes_inside_it(client, monkeypatch):
    import app.collectors.base as base_mod

    lock_during_write: list[bool] = []
    real_upsert_hosts = base_mod.upsert_hosts

    def spying_upsert_hosts(db, platform, hosts, instance):
        lock_during_write.append(write_lock.locked())
        return real_upsert_hosts(db, platform, hosts, instance)

    monkeypatch.setattr(base_mod, "upsert_hosts", spying_upsert_hosts)
    collector = _PhaseCollector(ServerConfig(name=_tag("PHASE"), platform="zabbix"), get_settings())

    db = SessionLocal()
    try:
        run = collector.run(db)
        assert run.status.value == "success"
        run2 = collector.run(db)
        assert run2.status.value == "success"
    finally:
        db.close()

    assert collector.lock_during_fetch is False, "network fetch must not hold the write lock"
    assert lock_during_write == [True, True], "every upsert must run under the write lock"
    # First run: full history window (since=None). Second run within the
    # refresh window: no history fetch at all; the timestamp only advances
    # after a successful write.
    assert collector.history_since == [None]
    assert collector._last_history_backfill is not None


def test_zabbix_history_window_is_incremental_after_the_first_pass(monkeypatch):
    settings = get_settings()
    collector = ZabbixCollector(ServerConfig(name=_tag("ZBX"), platform="zabbix", url="https://zbx.example"), settings)
    captured: list[dict] = []
    monkeypatch.setattr(collector, "_rpc", lambda method, params: captured.append(params) or [])
    monkeypatch.setattr(settings, "mock_mode", False)

    collector.collect_resolved_alerts(since=None)
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    collector.collect_resolved_alerts(since=since)

    full_from, incremental_from = captured[0]["time_from"], captured[1]["time_from"]
    days = settings.alert_history_days
    assert abs(full_from - (datetime.now(timezone.utc).timestamp() - days * 86400)) < 5
    assert abs(incremental_from - (since.timestamp() - 2 * 3600)) < 5
    assert incremental_from > full_from


# --- reconciliation + history backfill cost ---------------------------------


def _alert(external_id: str, title: str, started: datetime, **extra) -> dict:
    return {
        "external_id": external_id, "host_hostname": "perf-db01", "severity_int": 4,
        "severity_label": "High", "title": title, "started_at": started, **extra,
    }


def test_rereported_resolved_alert_is_reopened_in_place(client):
    inst = _tag("RECON")
    db = SessionLocal()
    try:
        upsert_alerts(db, SourcePlatform.zabbix, [_alert("A1", "cpu high", NOW)], inst)
        upsert_alerts(db, SourcePlatform.zabbix, [], inst)  # gone -> resolved
        db.commit()
        row = db.scalars(select(Alert).where(Alert.source_instance == inst)).one()
        assert row.resolved is True

        upsert_alerts(db, SourcePlatform.zabbix, [_alert("A1", "cpu high", NOW)], inst)
        db.commit()
        rows = db.scalars(select(Alert).where(Alert.source_instance == inst)).all()
        assert len(rows) == 1 and rows[0].resolved is False  # reopened, not duplicated
    finally:
        db.close()


def test_backfill_leaves_already_resolved_history_rows_untouched(client):
    inst = _tag("HIST")
    db = SessionLocal()
    try:
        old = NOW - timedelta(days=3)
        assert upsert_resolved_alerts(db, SourcePlatform.zabbix, [_alert("ev-1", "disk full", old)], inst) == 1
        db.commit()
        row = db.scalars(select(Alert).where(Alert.source_instance == inst)).one()
        stamp = row.updated_at

        assert upsert_resolved_alerts(db, SourcePlatform.zabbix, [_alert("ev-1", "renamed", old)], inst) == 0
        db.commit()
        db.refresh(row)
        assert row.title == "disk full" and row.updated_at == stamp  # closed history never rewritten
    finally:
        db.close()


# --- correlation coverage ----------------------------------------------------


def _open_events(db, n: int) -> list[int]:
    tag = _tag("COVER")
    entity = CanonicalEntity(entity_type=EntityType.host, canonical_name=tag)
    db.add(entity)
    db.flush()
    ids = []
    for i in range(n):
        ev = LogicalEvent(
            fingerprint=f"{tag}-{i}", entity_id=entity.id, normalized_problem_type=tag,
            status=LogicalEventStatus.open, last_seen=NOW - timedelta(seconds=i), title=tag,
        )
        db.add(ev)
        db.flush()
        ids.append(ev.id)
    return ids


def _stamped(db) -> set[int]:
    return set(db.scalars(select(LogicalEvent.id).where(LogicalEvent.correlated_at.is_not(None))).all())


def test_correlation_batch_walks_every_open_event_in_rounds(client):
    db = SessionLocal()
    try:
        _open_events(db, 6)
        db.commit()
        before = _stamped(db)

        run_correlation_batch(db, limit=3)
        db.commit()
        first = _stamped(db) - before
        assert len(first) == 3

        run_correlation_batch(db, limit=3)
        db.commit()
        second = _stamped(db) - before - first
        assert len(second) == 3 and not (second & first), "a second run must move on, not re-pick"
    finally:
        db.close()


def test_correlation_batch_honours_its_time_budget(client):
    db = SessionLocal()
    try:
        _open_events(db, 3)
        db.commit()
        result = run_correlation_batch(db, limit=3, time_budget_seconds=0)
        db.commit()
        assert result["events_processed"] == 1  # finishes what it started, takes nothing more
        assert "correlation_ids" in result
    finally:
        db.close()


# --- scheduler orchestration -------------------------------------------------


def test_run_all_skips_while_a_sweep_is_already_in_flight(monkeypatch):
    from app.scheduler import CollectorService

    service = CollectorService(get_settings())
    calls: list[str] = []
    monkeypatch.setattr(service, "run_one", lambda instance: calls.append(instance) or True)
    monkeypatch.setattr("app.scheduler.maybe_bootstrap_capacity", lambda: None)
    service.collectors = {"X": object()}  # type: ignore[assignment]

    with service._run_all_lock:
        service.run_all()  # a manual "Refresh now" mid-sweep
    assert calls == []
    service.run_all()
    assert calls == ["X"]


def test_topology_run_is_followed_by_the_relationship_sync(monkeypatch):
    import app.scheduler as sched
    import app.topology_sync as ts

    order: list[str] = []
    monkeypatch.setattr(sched, "run_topology", lambda settings: order.append("collect"))
    monkeypatch.setattr(ts, "sync_all", lambda db: order.append(f"sync(locked={write_lock.locked()})") or {})

    sched.run_topology_now()
    assert order == ["collect", "sync(locked=True)"]
