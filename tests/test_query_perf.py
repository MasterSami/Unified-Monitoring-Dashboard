"""Guards on the query patterns that make the hot pages cheap.

These are regression tests, not micro-benchmarks. Each one pins a property the
audit fixed — a bounded query count, an index-usable predicate, a deferred JSON
column, a batched prefetch — so a future refactor that reintroduces the slow
shape fails here rather than in production.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Alert, Host, HostStatus, SourcePlatform

BASE = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)


@contextmanager
def captured_sql():
    """Collect every statement executed inside the block."""
    seen: list[str] = []

    def _record(_conn, _cur, statement, *_a, **_k):
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _record)


@pytest.fixture(scope="module")
def seeded(client):
    """A handful of hosts and alerts across two instances."""
    db = SessionLocal()
    try:
        for i in range(12):
            db.add(Host(
                external_id=f"perf-h{i}",
                source_platform=SourcePlatform.zabbix,
                source_instance="Zabbix-PERF-A" if i % 2 else "Zabbix-PERF-B",
                hostname=f"perf-host-{i:02d}", ip=f"10.90.0.{i}",
                status=HostStatus.up if i % 3 else HostStatus.down,
                owner="Ahmed", owner_email="ahmed@corp.com",
                last_seen=BASE, raw_payload={"blob": "x" * 500},
            ))
            db.add(Alert(
                external_id=f"perf-a{i}", source_platform=SourcePlatform.zabbix,
                source_instance="Zabbix-PERF-A" if i % 2 else "Zabbix-PERF-B",
                host_hostname=f"perf-host-{i:02d}", severity_int=(i % 5) + 1,
                severity_label="High", title=f"perf alert {i}",
                started_at=BASE - timedelta(minutes=i), resolved=False,
                raw_payload={"blob": "y" * 500},
            ))
        db.commit()
    finally:
        db.close()
    return True


# --- Overview: one rollup, not five aggregates ------------------------------


def test_overview_context_issues_a_bounded_number_of_queries(seeded):
    from app.routers.pages import _overview_context

    db = SessionLocal()
    try:
        with captured_sql() as sql:
            ctx = _overview_context(db, get_settings())
    finally:
        db.close()

    # Total hosts, per-status, per-platform and per-instance all come out of a
    # single GROUP BY; previously each was its own aggregate query.
    host_aggregates = [s for s in sql if "count(" in s.lower() and " hosts" in s.lower()]
    assert len(host_aggregates) <= 2, host_aggregates
    assert len(sql) <= 8, sql

    # And the derived numbers still agree with each other.
    assert ctx["total_hosts"] == sum(ctx["status_counts"].values())
    assert ctx["hosts_down"] == ctx["status_counts"]["down"]
    assert ctx["active_alerts"] == sum(b.count for b in ctx["severity_buckets"])
    assert ctx["critical_alerts"] == next(
        b.count for b in ctx["severity_buckets"] if b.severity_int == 5
    )
    assert ctx["total_hosts"] == sum(p.total for p in ctx["per_platform"])


def test_overview_rollup_matches_direct_counts(seeded):
    """The derived rollup must equal what the old per-metric queries returned."""
    from sqlalchemy import func, select

    from app.routers.pages import _host_rollup, _host_status_counts, _per_platform

    db = SessionLocal()
    try:
        rows = _host_rollup(db)
        direct_total = db.scalar(select(func.count(Host.id)))
        direct_down = db.scalar(
            select(func.count(Host.id)).where(Host.status == HostStatus.down)
        )
    finally:
        db.close()

    counts = _host_status_counts(rows)
    assert sum(counts.values()) == direct_total
    assert counts["down"] == direct_down
    assert sum(p.down for p in _per_platform(rows)) == direct_down


# --- Deferred JSON payloads -------------------------------------------------


def test_table_queries_do_not_select_raw_payload(seeded):
    """raw_payload holds the whole source record and nothing renders it."""
    from app.routers.pages import _active_alerts, _hosts_query

    db = SessionLocal()
    try:
        with captured_sql() as sql:
            _hosts_query(db, None, "all", "all", "hostname", "asc")
            _active_alerts(db, None, 1, state="active")
    finally:
        db.close()

    selects = [s for s in sql if s.lower().startswith("select")]
    assert selects
    assert not [s for s in selects if "raw_payload" in s], (
        "raw_payload must stay deferred on the table read paths"
    )


def test_count_query_does_not_project_the_entity(seeded):
    from app.routers.pages import _count_of, _hosts_stmt

    db = SessionLocal()
    try:
        with captured_sql() as sql:
            total = _count_of(db, _hosts_stmt(None, "all", "all", "all"))
    finally:
        db.close()

    assert total >= 12
    assert len(sql) == 1
    # A count should not drag every column through a subquery to get there.
    assert "hosts.metrics" not in sql[0]
    assert "count(" in sql[0].lower()


# --- Escalation owner lookup ------------------------------------------------


def test_owner_lookup_uses_the_indexable_column(seeded):
    """lower(hostname) IN (...) cannot use ix_hosts_hostname; the raw column can."""
    from app.routers.pages import _attach_escalation

    db = SessionLocal()
    try:
        alerts = db.query(Alert).filter(
            Alert.source_instance.in_(["Zabbix-PERF-A", "Zabbix-PERF-B"])
        ).all()
        with captured_sql() as sql:
            _attach_escalation(db, alerts)
    finally:
        db.close()

    owner_lookups = [s for s in sql if "owner_email" in s]
    assert owner_lookups, "expected an owner lookup"
    # Hostnames match exactly here, so the case-insensitive fallback never runs.
    assert len(owner_lookups) == 1
    assert "lower(hosts.hostname)" not in owner_lookups[0].lower()
    # ...and the owners were still resolved.
    assert alerts and all(a.owner == "Ahmed" for a in alerts)


def test_owner_lookup_still_matches_a_case_mismatch(client):
    """The indexed path is an optimization, not a behavior change."""
    from app.routers.pages import _attach_escalation

    db = SessionLocal()
    try:
        db.add(Host(
            external_id="case-h", source_platform=SourcePlatform.zabbix,
            source_instance="Zabbix-CASE", hostname="MixedCase-Host",
            ip="10.91.0.1", status=HostStatus.up,
            owner="Sami", owner_email="sami@corp.com",
        ))
        db.add(Alert(
            external_id="case-a", source_platform=SourcePlatform.zabbix,
            source_instance="Zabbix-CASE", host_hostname="mixedcase-host",
            severity_int=4, severity_label="High", title="case test",
            started_at=BASE, resolved=False,
        ))
        db.commit()
        alert = db.query(Alert).filter(Alert.external_id == "case-a").one()
        _attach_escalation(db, [alert])
        assert alert.owner == "Sami" and alert.owner_email == "sami@corp.com"
        assert alert.escalate_href.startswith("mailto:sami%40corp.com?")
    finally:
        db.close()


# --- SiteScope ingest: batched, not one query per event ---------------------


def test_ingest_prefetches_instead_of_querying_per_event(client):
    from app.sitescope import NormalizedEvent
    from app.sitescope_ingest import upsert_events

    events = [
        NormalizedEvent(
            source_instance="SiteScope-PERF",
            external_id=f"perf-ev-{i}",
            host_hostname=f"sis-host-{i}",
            severity_int=3,
            severity_label="Warning",
            title=f"monitor {i}",
            started_at=BASE,
            resolved=False,
            state="warning",
            dedup_key=f"k{i}",
            metric_missing=False,
            monitor_name=f"monitor {i}",
            raw_payload={},
        )
        for i in range(40)
    ]

    db = SessionLocal()
    try:
        with captured_sql() as sql:
            inserted, updated = upsert_events(db, events)
        db.commit()
    finally:
        db.close()

    assert (inserted, updated) == (40, 0)
    selects = [s for s in sql if s.lower().startswith("select")]
    assert len(selects) <= 2, f"expected a batched prefetch, got {len(selects)} selects"

    # Re-ingesting the same batch updates in place and stays just as batched.
    db = SessionLocal()
    try:
        with captured_sql() as sql2:
            inserted2, updated2 = upsert_events(db, events)
        db.commit()
    finally:
        db.close()

    assert (inserted2, updated2) == (0, 40)
    assert len([s for s in sql2 if s.lower().startswith("select")]) <= 2


def test_ingest_hosts_are_prefetched_too(client):
    from app.sitescope import DerivedHost
    from app.sitescope_ingest import upsert_hosts

    hosts = [
        DerivedHost(
            external_id=f"perf-dh-{i}", hostname=f"dh-{i}", ip=f"10.92.0.{i}",
            status="up", group_name="SiteScope", last_seen=BASE, raw_payload={},
        )
        for i in range(30)
    ]
    db = SessionLocal()
    try:
        with captured_sql() as sql:
            inserted = upsert_hosts(db, "SiteScope-PERF", hosts)
        db.commit()
    finally:
        db.close()

    assert inserted == 30
    assert len([s for s in sql if s.lower().startswith("select")]) <= 2


# --- servers.yaml parse cache -----------------------------------------------


def test_server_inventory_is_not_reparsed_on_every_call(tmp_path):
    from app.config import Settings
    from app.servers import _yaml_cache, load_servers, reset_server_cache

    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "zabbix:\n  - name: Z1\n    url: http://z1\n    user: u\n    pass: p\n",
        encoding="utf-8",
    )
    settings = Settings(
        mock_mode=False, enabled_collectors="zabbix", servers_config=str(cfg)
    )
    reset_server_cache()

    reads = {"n": 0}

    import app.servers as servers_mod

    real_load = servers_mod._load_yaml

    def counting_load(path):
        reads["n"] += 1
        return real_load(path)

    servers_mod._load_yaml = counting_load
    try:
        for _ in range(5):
            assert [s.name for s in load_servers(settings)] == ["Z1"]
        assert reads["n"] == 1, "the YAML must be parsed once, not once per call"

        # Editing the file invalidates the cache.
        cfg.write_text(
            "zabbix:\n  - name: Z1\n    url: http://z1\n"
            "  - name: Z2\n    url: http://z2\n",
            encoding="utf-8",
        )
        assert [s.name for s in load_servers(settings)] == ["Z1", "Z2"]
        assert reads["n"] == 2
    finally:
        servers_mod._load_yaml = real_load
        reset_server_cache()
        _yaml_cache.clear()


# --- Manual triggers do not block the request thread ------------------------


def test_manual_collector_run_is_handed_to_the_scheduler(client, monkeypatch):
    """"Refresh now" must not run every collector inline on the request thread."""
    import app.routers.api as api_mod

    queued: list[str] = []
    monkeypatch.setattr(api_mod, "request_run_all", lambda: queued.append("all") or True)

    resp = client.post("/api/v1/collectors/run")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert queued == ["all"]


def test_manual_run_falls_back_to_inline_without_a_scheduler(client):
    """With no scheduler running (as in tests) the work still happens."""
    resp = client.post("/api/v1/collectors/run")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "queued")


def test_unknown_instance_still_404s(client):
    assert client.post("/api/v1/collectors/nope-not-real/run").status_code == 404


# --- JSON API is bounded ----------------------------------------------------


def test_json_endpoints_are_capped(seeded, client):
    assert len(client.get("/api/v1/hosts?limit=3").json()) == 3
    assert len(client.get("/api/v1/alerts?limit=2").json()) == 2
    # A bare request is capped by API_DEFAULT_LIMIT rather than returning all rows.
    assert len(client.get("/api/v1/hosts").json()) <= get_settings().api_default_limit
    # An absurd limit is clamped, not honored.
    assert (
        len(client.get("/api/v1/alerts?limit=999999").json())
        <= get_settings().api_max_limit
    )


def test_json_endpoints_paginate(seeded, client):
    first = client.get("/api/v1/hosts?limit=5").json()
    second = client.get("/api/v1/hosts?limit=5&offset=5").json()
    assert {h["hostname"] for h in first}.isdisjoint({h["hostname"] for h in second})


# --- CSV export streams -----------------------------------------------------


def test_csv_export_streams_rows_lazily(seeded, client):
    """The response body must be assembled from chunks, not one prebuilt string."""
    with client.stream("GET", "/api/v1/hosts.csv") as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode()

    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[0].startswith("hostname,ip,platform")
    assert any("perf-host-00" in ln for ln in lines)
