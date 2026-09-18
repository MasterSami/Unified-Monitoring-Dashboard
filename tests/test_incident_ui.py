"""Correlation Phase 6: operational UI + production hardening.

Covers the Incidents list/detail pages (rendering + the feature flag),
the extended /api/v1/incidents filters, the wider correlation-graph node
set (healthy_dependency), operator feedback (auth-gated, audited), the
correlation metrics endpoint, the /api/v1/servers reference list, and the
scheduler's background correlation/incident job.

Rules/entities are GLOBAL across the whole test session — every test below
that creates one uses a unique tag (via _tag()), same discipline as
test_correlation_engine.py and test_incident_engine.py.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import metrics as metrics_mod
from app.correlation_engine import correlate_pair
from app.correlation_rules import create_rule
from app.db import SessionLocal
from app.dependency_graph import record_relationship
from app.incident_engine import form_or_update_incident_from_correlation, recompute_incident
from app.models import (
    AuditLog,
    CanonicalEntity,
    Correlation,
    EntityType,
    Incident,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    TopologySource,
)
from tests.conftest import RUNBOOK_PASSWORD, RUNBOOK_USER

NOW = datetime(2026, 9, 20, 9, 0, 0, tzinfo=timezone.utc)

_tag_counter = itertools.count(1)


def _tag(label: str) -> str:
    return f"{label}_{next(_tag_counter)}"


def _entity(db, entity_type: EntityType, name: str) -> int:
    e = CanonicalEntity(entity_type=entity_type, canonical_name=name)
    db.add(e)
    db.flush()
    return e.id


def _event(db, *, entity_id, problem_type, last_seen, status=LogicalEventStatus.open) -> LogicalEvent:
    fp = f"entity:{entity_id}|problem:{problem_type}|__test__:{id(object())}"
    le = LogicalEvent(
        fingerprint=fp, entity_id=entity_id, normalized_problem_type=problem_type,
        status=status, occurrence_count=1, first_seen=last_seen, last_seen=last_seen,
        sources=[], title=problem_type, current_severity_int=4, current_severity_label="High",
    )
    db.add(le)
    db.flush()
    return le


def _login(client) -> None:
    client.cookies.clear()
    client.post(
        "/runbook/login", data={"username": RUNBOOK_USER, "password": RUNBOOK_PASSWORD},
        follow_redirects=True,
    )


def _make_incident(db, *, database_name, service_name):
    database = _entity(db, EntityType.database, database_name)
    service = _entity(db, EntityType.service, service_name)
    record_relationship(
        db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
        from_entity_id=service, to_entity_id=database,
    )
    tag = _tag("UI_DB")
    ev_db = _event(db, entity_id=database, problem_type=tag, last_seen=NOW)
    ev_svc = _event(db, entity_id=service, problem_type=_tag("UI_SVC"), last_seen=NOW + timedelta(seconds=5))
    db.flush()
    create_rule(
        db, rule_id=f"UI-{tag}", name="ui test rule",
        conditions=[{"event_type": tag}, {"relationship": "depends_on"}, {"time_window_seconds": 900}],
    )
    outcome = correlate_pair(db, ev_db.id, ev_svc.id)
    assert outcome.correlated is True
    db.flush()
    correlation = db.get(Correlation, outcome.correlation_id)
    incident = form_or_update_incident_from_correlation(db, correlation)
    db.commit()
    return incident, ev_db, ev_svc, database, service


class TestPagesRender:
    def test_incidents_list_page_disabled_by_default(self, client):
        r = client.get("/incidents")
        assert r.status_code == 200
        assert "turned off" in r.text

    def test_incidents_list_page_renders_when_enabled(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_incidents", True)
        r = client.get("/incidents")
        assert r.status_code == 200
        assert "ic-status" in r.text  # the filter bar

    def test_incident_detail_page_renders_shell(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_incidents", True)
        r = client.get("/incidents/1")
        assert r.status_code == 200
        assert "Incident #1" in r.text
        assert "ic-detail-root" in r.text

    def test_nav_shows_coming_soon_when_disabled(self, client):
        # enable_incidents is baked into the Jinja globals at process start
        # (same convention as every other optional-tab flag - see
        # app.routers.pages's own module-level globals block), so it can't
        # be flipped per-test via monkeypatch; only its default-off state is
        # exercised here, same as the route-level checks above cover "on".
        body = client.get("/").text
        assert "ENABLE_INCIDENTS" in body


class TestIncidentListFilters:
    def test_severity_min_filters_out_lower_severity(self, client):
        db = SessionLocal()
        try:
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=_tag("FilterDB"), service_name=_tag("FilterSvc"),
            )
            incident_id = incident.id
        finally:
            db.close()

        r = client.get("/api/v1/incidents", params={"event_id": ev_db.id, "severity_min": 4})
        assert any(i["id"] == incident_id for i in r.json())

        r = client.get("/api/v1/incidents", params={"event_id": ev_db.id, "severity_min": 5})
        assert not any(i["id"] == incident_id for i in r.json())

    def test_database_and_service_substring_filters(self, client):
        db = SessionLocal()
        try:
            db_name = _tag("SubstrDB")
            svc_name = _tag("SubstrSvc")
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=db_name, service_name=svc_name,
            )
            incident_id = incident.id
        finally:
            db.close()

        r = client.get("/api/v1/incidents", params={"database": db_name[:8]})
        assert any(i["id"] == incident_id for i in r.json())

        r = client.get("/api/v1/incidents", params={"service": svc_name[:8]})
        assert any(i["id"] == incident_id for i in r.json())

        r = client.get("/api/v1/incidents", params={"database": "no-such-database-xyz"})
        assert not any(i["id"] == incident_id for i in r.json())

    def test_root_cause_filter(self, client):
        db = SessionLocal()
        try:
            db_name = _tag("RCFilterDB")
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=db_name, service_name=_tag("RCFilterSvc"),
            )
            incident_id = incident.id
        finally:
            db.close()

        r = client.get("/api/v1/incidents", params={"root_cause": db_name[:8]})
        assert any(i["id"] == incident_id for i in r.json())

    def test_start_time_range_filter(self, client):
        db = SessionLocal()
        try:
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=_tag("TimeDB"), service_name=_tag("TimeSvc"),
            )
            incident_id = incident.id
        finally:
            db.close()

        far_future = (NOW + timedelta(days=3650)).isoformat()
        r = client.get("/api/v1/incidents", params={"event_id": ev_db.id, "start_from": far_future})
        assert not any(i["id"] == incident_id for i in r.json())

        far_past = (NOW - timedelta(days=3650)).isoformat()
        r = client.get("/api/v1/incidents", params={"event_id": ev_db.id, "start_from": far_past})
        assert any(i["id"] == incident_id for i in r.json())

    def test_pagination_limit_offset(self, client):
        r = client.get("/api/v1/incidents", params={"limit": 1, "offset": 0})
        assert r.status_code == 200
        assert len(r.json()) <= 1


class TestCorrelationGraphHealthyDependency:
    def test_graph_includes_healthy_dependency_node(self, client):
        db = SessionLocal()
        try:
            db_name = _tag("GraphDB")
            svc_name = _tag("GraphSvc")
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=db_name, service_name=svc_name,
            )
            # A THIRD entity that depends on the service (so it's downstream
            # of the root cause chain) but has no event of its own -> should
            # surface as "healthy_dependency", not vanish from the graph.
            api = _entity(db, EntityType.api, _tag("GraphApi"))
            record_relationship(
                db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                from_entity_id=api, to_entity_id=service,
            )
            db.commit()
            recompute_incident(db, incident)
            db.commit()
            incident_id = incident.id
        finally:
            db.close()

        r = client.get(f"/api/v1/incidents/{incident_id}/correlation-graph")
        assert r.status_code == 200
        graph = r.json()
        roles = {n["role"] for n in graph["nodes"]}
        assert "root_cause_candidate" in roles
        assert "symptom" in roles
        assert "healthy_dependency" in roles


class TestOperatorFeedback:
    def test_feedback_requires_login_and_is_recorded(self, client):
        db = SessionLocal()
        try:
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=_tag("FbDB"), service_name=_tag("FbSvc"),
            )
            incident_id = incident.id
        finally:
            db.close()

        client.cookies.clear()
        r = client.post(f"/api/v1/incidents/{incident_id}/feedback", json={"kind": "root_cause_correct"})
        assert r.status_code == 401

        _login(client)
        r = client.post(
            f"/api/v1/incidents/{incident_id}/feedback",
            json={"kind": "root_cause_correct", "note": "matches what we found"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["kind"] == "root_cause_correct"
        assert body["actor"] == RUNBOOK_USER

        r = client.get(f"/api/v1/incidents/{incident_id}/feedback")
        assert r.status_code == 200
        assert any(f["id"] == body["id"] for f in r.json())

        db = SessionLocal()
        try:
            audit_rows = db.scalars(
                select(AuditLog).where(AuditLog.action == "incident_feedback", AuditLog.target == f"incident:{incident_id}")
            ).all()
            assert audit_rows
        finally:
            db.close()

        client.cookies.clear()

    def test_unknown_feedback_kind_is_rejected(self, client):
        db = SessionLocal()
        try:
            incident, ev_db, ev_svc, database, service = _make_incident(
                db, database_name=_tag("BadKindDB"), service_name=_tag("BadKindSvc"),
            )
            incident_id = incident.id
        finally:
            db.close()

        _login(client)
        r = client.post(f"/api/v1/incidents/{incident_id}/feedback", json={"kind": "not_a_real_kind"})
        assert r.status_code == 422
        client.cookies.clear()


class TestCorrelationMetrics:
    def test_metrics_endpoint_shape_and_live_counts(self, client):
        db = SessionLocal()
        try:
            before = db.scalar(select(Incident.id).limit(1))
            _make_incident(db, database_name=_tag("MetricsDB"), service_name=_tag("MetricsSvc"))
        finally:
            db.close()

        r = client.get("/api/v1/metrics/correlation")
        assert r.status_code == 200
        body = r.json()
        for key in (
            "events_received", "events_normalized", "events_deduplicated", "correlations_created",
            "incidents_created", "incidents_merged", "root_cause_candidates", "correlation_failures",
            "average_processing_time",
        ):
            assert key in body
        assert body["incidents_created"] >= 1
        assert body["correlations_created"] >= 1

    def test_recorded_failure_and_duration_show_up_in_metrics(self, client):
        db = SessionLocal()
        try:
            metrics_mod.increment_counter(db, "correlation_failures")
            metrics_mod.record_duration(db, "correlation_duration_seconds", 2.5)
            metrics_mod.record_duration(db, "correlation_duration_seconds", 1.5)
            db.commit()
        finally:
            db.close()

        r = client.get("/api/v1/metrics/correlation")
        body = r.json()
        assert body["correlation_failures"] >= 1
        assert body["average_processing_time"] > 0


class TestServersReference:
    def test_servers_endpoint_never_leaks_credentials(self, client):
        r = client.get("/api/v1/servers")
        assert r.status_code == 200
        for row in r.json():
            assert set(row.keys()) == {"name", "platform", "url"}


class TestBackgroundJob:
    def test_correlation_and_incident_job_runs_without_raising(self, client):
        from app.scheduler import _run_correlation_and_incidents_job

        db = SessionLocal()
        try:
            _make_incident(db, database_name=_tag("JobDB"), service_name=_tag("JobSvc"))
        finally:
            db.close()

        _run_correlation_and_incidents_job()  # must not raise
