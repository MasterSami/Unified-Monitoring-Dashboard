"""Correlation Phase 7: AI-readiness (architecture preparation only).

No AI/ML runs anywhere in this codebase — these tests cover the structured
historical data Phases 1-6 now preserve (correlation evidence with explicit
from/to entities and the triggering rule, operator feedback with a
confirmed root cause, incident resolution), the read-only historical
export, and — just as importantly — that app.ai_provider is a pure,
unimplemented interface the deterministic engine never imports or depends
on.

CorrelationRule rows are GLOBAL and shared across the whole test session —
same discipline as test_correlation_engine.py/test_incident_engine.py:
every rule below is scoped with a unique event_type tag.
"""

from __future__ import annotations

import inspect
import itertools
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.ai_provider import AIAnalysisProvider, get_ai_provider
from app.correlation_engine import correlate_pair
from app.correlation_rules import create_rule
from app.db import SessionLocal
from app.dependency_graph import record_relationship
from app.incident_engine import form_or_update_incident_from_correlation
from app.incident_history import export_incident_history
from app.models import (
    Alert,
    AuditLog,
    CanonicalEntity,
    Correlation,
    CorrelationEvidence,
    EntityType,
    FeedbackKind,
    IncidentFeedback,
    IncidentResolution,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    SourcePlatform,
    TopologySource,
)
from tests.conftest import RUNBOOK_PASSWORD, RUNBOOK_USER

NOW = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

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


def _occurrence(db, *, logical_event: LogicalEvent, platform: SourcePlatform = SourcePlatform.zabbix) -> Alert:
    a = Alert(
        source_platform=platform, source_instance="T1", external_id=f"occ-{id(object())}",
        severity_int=4, severity_label="High", title=logical_event.normalized_problem_type,
        started_at=logical_event.last_seen, resolved=False,
        entity_id=logical_event.entity_id, logical_event_id=logical_event.id,
    )
    db.add(a)
    db.flush()
    return a


def _login(client) -> None:
    client.cookies.clear()
    client.post(
        "/runbook/login", data={"username": RUNBOOK_USER, "password": RUNBOOK_PASSWORD},
        follow_redirects=True,
    )


class TestAIProviderInterfaceIsUnimplemented:
    def test_get_ai_provider_returns_none(self, client):
        assert get_ai_provider() is None

    def test_provider_cannot_be_instantiated_directly(self, client):
        import pytest

        with pytest.raises(TypeError):
            AIAnalysisProvider()  # type: ignore[abstract]

    def test_provider_has_exactly_the_task_named_methods(self, client):
        expected = {
            "find_similar_incidents", "suggest_root_cause", "detect_patterns",
            "suggest_correlation_rule", "summarize_incident",
        }
        abstract = {
            name for name, member in inspect.getmembers(AIAnalysisProvider)
            if getattr(member, "__isabstractmethod__", False)
        }
        assert abstract == expected

    def test_engine_modules_never_import_ai_provider(self, client):
        """Acceptance criterion: the deterministic engine's correctness can
        never depend on an AI layer that might not exist. Grepping the
        source is the most direct proof there is zero coupling, not just
        that AI "isn't called" at runtime.
        """
        import app.correlation_engine as ce
        import app.incident_engine as ie

        assert "ai_provider" not in inspect.getsource(ce)
        assert "ai_provider" not in inspect.getsource(ie)


class TestStructuredCorrelationEvidence:
    def test_known_dependency_evidence_carries_the_real_relationship_direction(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, _tag("StructDB"))
            service = _entity(db, EntityType.service, _tag("StructSvc"))
            record_relationship(
                db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                from_entity_id=service, to_entity_id=database,
            )
            tag = _tag("STRUCT_EVIDENCE")
            create_rule(
                db, rule_id=f"STRUCT-{tag}", name="structured evidence test",
                conditions=[{"event_type": tag}, {"relationship": "depends_on"}, {"time_window_seconds": 900}],
            )
            # Correlate with the DATABASE event as `a` and the SERVICE event
            # as `b` — the OPPOSITE of the relationship's own from/to — to
            # prove the stored evidence reflects the real topology
            # direction, not just whichever event happened to be passed first.
            ev_db = _event(db, entity_id=database, problem_type=tag, last_seen=NOW)
            ev_svc = _event(db, entity_id=service, problem_type=_tag("STRUCT_SVC"), last_seen=NOW + timedelta(seconds=5))
            db.commit()

            outcome = correlate_pair(db, ev_db.id, ev_svc.id)
            assert outcome.correlated is True
            db.commit()

            evidence = db.scalars(
                select(CorrelationEvidence).where(
                    CorrelationEvidence.correlation_id == outcome.correlation_id,
                    CorrelationEvidence.signal == "known_dependency",
                )
            ).all()
            assert evidence
            row = evidence[0]
            assert row.from_entity_id == service
            assert row.to_entity_id == database
            assert row.rule_id == f"STRUCT-{tag}"
        finally:
            db.close()

    def test_non_directional_signal_falls_back_to_the_compared_events_own_entities(self, client):
        db = SessionLocal()
        try:
            entity = _entity(db, EntityType.service, _tag("SharedEntity"))
            tag = _tag("SAME_ENTITY_EVIDENCE")
            create_rule(
                db, rule_id=f"SAMEENT-{tag}", name="same entity",
                conditions=[{"event_type": tag}, {"signals": ["same_entity"]}, {"time_window_seconds": 900}],
            )
            ev_a = _event(db, entity_id=entity, problem_type=tag, last_seen=NOW)
            ev_b = _event(db, entity_id=entity, problem_type=_tag("SAME_ENTITY_B"), last_seen=NOW + timedelta(seconds=3))
            db.commit()

            outcome = correlate_pair(db, ev_a.id, ev_b.id)
            assert outcome.correlated is True
            db.commit()

            row = db.scalar(
                select(CorrelationEvidence).where(
                    CorrelationEvidence.correlation_id == outcome.correlation_id,
                    CorrelationEvidence.signal == "same_entity",
                )
            )
            assert row is not None
            assert row.from_entity_id == entity
            assert row.to_entity_id == entity
        finally:
            db.close()

    def test_rules_triggered_are_preserved_even_after_correlation_rule_id_is_overwritten(self, client):
        """Correlation.rule_id only ever holds the MOST RECENT rule — the
        evidence trail is the only place the full history of every rule
        that ever fired survives (task section 1: "rules triggered").
        """
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, _tag("HistDB"))
            svc_a = _entity(db, EntityType.service, _tag("HistSvcA"))
            svc_b = _entity(db, EntityType.service, _tag("HistSvcB"))
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc_a, to_entity_id=database)
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc_b, to_entity_id=database)

            tag_a = _tag("HIST_A")
            create_rule(db, rule_id=f"HIST-RULE-A-{tag_a}", name="rule a",
                        conditions=[{"event_type": tag_a}, {"relationship": "depends_on"}, {"time_window_seconds": 900}])
            ev_db = _event(db, entity_id=database, problem_type=tag_a, last_seen=NOW)
            ev_a = _event(db, entity_id=svc_a, problem_type=_tag("HIST_SVC_A"), last_seen=NOW + timedelta(seconds=5))
            db.commit()
            out1 = correlate_pair(db, ev_db.id, ev_a.id)
            assert out1.correlated is True
            db.commit()

            tag_b = _tag("HIST_B")
            create_rule(db, rule_id=f"HIST-RULE-B-{tag_b}", name="rule b",
                        conditions=[{"event_type": tag_b}, {"relationship": "depends_on"}, {"time_window_seconds": 900}])
            # Retag ev_db so the SECOND rule (scoped to tag_b) also matches
            # it against svc_b - proves a DIFFERENT rule contributing to the
            # SAME correlation group is preserved in the evidence trail.
            ev_db.normalized_problem_type = tag_b
            db.flush()
            ev_b = _event(db, entity_id=svc_b, problem_type=_tag("HIST_SVC_B"), last_seen=NOW + timedelta(seconds=8))
            db.commit()
            out2 = correlate_pair(db, ev_db.id, ev_b.id)
            assert out2.correlated is True
            assert out2.correlation_id == out1.correlation_id
            db.commit()

            correlation = db.get(Correlation, out1.correlation_id)
            # Correlation.rule_id now only shows the LATEST rule.
            assert correlation.rule_id == f"HIST-RULE-B-{tag_b}"

            incident = form_or_update_incident_from_correlation(db, correlation)
            db.commit()

            history = export_incident_history(db, incident.id)
            assert set(history["rules_triggered"]) == {f"HIST-RULE-A-{tag_a}", f"HIST-RULE-B-{tag_b}"}
        finally:
            db.close()


class TestOperatorFeedbackConfirmedRootCause:
    def test_feedback_stores_confirmed_root_cause_entity(self, client):
        incident_id, db_entity_id = _build_simple_incident(_tag("FbConfirmDB"), _tag("FbConfirmSvc"))
        _login(client)
        r = client.post(
            f"/api/v1/incidents/{incident_id}/feedback",
            json={"kind": "root_cause_correct", "confirmed_root_cause_entity_id": db_entity_id},
        )
        assert r.status_code == 201
        assert r.json()["confirmed_root_cause_entity_id"] == db_entity_id

        db = SessionLocal()
        try:
            row = db.scalar(select(IncidentFeedback).where(IncidentFeedback.incident_id == incident_id))
            assert row.confirmed_root_cause_entity_id == db_entity_id
        finally:
            db.close()
        client.cookies.clear()


class TestIncidentResolution:
    def test_resolution_requires_login_then_upserts_and_is_audited(self, client):
        incident_id, db_entity_id = _build_simple_incident(_tag("ResDB"), _tag("ResSvc"))

        client.cookies.clear()
        r = client.get(f"/api/v1/incidents/{incident_id}/resolution")
        assert r.status_code == 404

        r = client.post(f"/api/v1/incidents/{incident_id}/resolution", json={"resolution_action": "restarted it"})
        assert r.status_code == 401

        _login(client)
        r = client.post(
            f"/api/v1/incidents/{incident_id}/resolution",
            json={
                "confirmed_root_cause_entity_id": db_entity_id,
                "resolution_action": "restarted the DB connection pool",
                "resolver": "dba-team",
                "post_incident_notes": "pool size was too small for peak load",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["confirmed_root_cause_entity_id"] == db_entity_id
        assert body["resolution_action"] == "restarted the DB connection pool"
        first_id = body["id"]

        # Submitting again UPDATES the same row, not a second one.
        r = client.post(
            f"/api/v1/incidents/{incident_id}/resolution",
            json={"resolution_action": "actually failed over to DR", "resolver": "dba-team"},
        )
        assert r.status_code == 201
        assert r.json()["id"] == first_id
        assert r.json()["resolution_action"] == "actually failed over to DR"

        r = client.get(f"/api/v1/incidents/{incident_id}/resolution")
        assert r.status_code == 200
        assert r.json()["resolution_action"] == "actually failed over to DR"

        db = SessionLocal()
        try:
            rows = db.scalars(select(IncidentResolution).where(IncidentResolution.incident_id == incident_id)).all()
            assert len(rows) == 1
            audit_rows = db.scalars(
                select(AuditLog).where(AuditLog.action == "incident_resolution", AuditLog.target == f"incident:{incident_id}")
            ).all()
            assert len(audit_rows) == 2
        finally:
            db.close()
        client.cookies.clear()

    def test_unknown_incident_resolution_404s(self, client):
        _login(client)
        r = client.post("/api/v1/incidents/999999999/resolution", json={"resolution_action": "n/a"})
        assert r.status_code == 404
        client.cookies.clear()


class TestHistoricalExport:
    def test_export_shape_covers_every_task_named_field(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, _tag("ExportDB"))
            service = _entity(db, EntityType.service, _tag("ExportSvc"))
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=service, to_entity_id=database)
            tag = _tag("EXPORT_DB")
            create_rule(db, rule_id=f"EXPORT-{tag}", name="export test",
                        conditions=[{"event_type": tag}, {"relationship": "depends_on"}, {"time_window_seconds": 900}])
            ev_db = _event(db, entity_id=database, problem_type=tag, last_seen=NOW)
            ev_svc = _event(db, entity_id=service, problem_type=_tag("EXPORT_SVC"), last_seen=NOW + timedelta(seconds=5))
            _occurrence(db, logical_event=ev_db)
            _occurrence(db, logical_event=ev_svc, platform=SourcePlatform.dynatrace)
            db.commit()

            outcome = correlate_pair(db, ev_db.id, ev_svc.id)
            assert outcome.correlated is True
            db.commit()

            incident = form_or_update_incident_from_correlation(db, db.get(Correlation, outcome.correlation_id))
            db.commit()

            db.add(IncidentFeedback(
                incident_id=incident.id, kind=FeedbackKind.root_cause_correct, actor="tester",
                confirmed_root_cause_entity_id=database,
            ))
            db.add(IncidentResolution(
                incident_id=incident.id, confirmed_root_cause_entity_id=database,
                resolution_action="restarted db", resolver="tester", resolution_time=NOW,
            ))
            db.commit()

            history = export_incident_history(db, incident.id)
            assert history is not None
            assert history["incident"]["id"] == incident.id
            assert {e["entity_id"] for e in history["entities"]} >= {database, service}
            assert any(t["from_entity_id"] == service and t["to_entity_id"] == database for t in history["topology"])
            assert history["correlation_signals"]
            assert all("from_entity" not in s for s in history["correlation_signals"])  # keys are from_entity_id/to_entity_id, not renamed
            assert history["rules_triggered"] == [f"EXPORT-{tag}"]
            assert history["root_cause_candidates"]
            assert history["root_cause_candidates"][0]["entity_id"] == database
            assert len(history["operator_confirmation"]) == 1
            assert history["operator_confirmation"][0]["confirmed_root_cause_entity_id"] == database
            assert history["resolution"]["confirmed_root_cause_entity_id"] == database
            assert history["resolution_time"] is not None
            assert history["business_impact"]["affected"] == incident.affected
            assert isinstance(history["affected_services"], list)
            assert isinstance(history["affected_apis"], list)
            assert isinstance(history["affected_databases"], list)
            assert history["timeline"]
            assert len(history["events"]) == 2
        finally:
            db.close()

    def test_export_returns_none_for_unknown_incident(self, client):
        db = SessionLocal()
        try:
            assert export_incident_history(db, 999999999) is None
        finally:
            db.close()

    def test_history_api_endpoint(self, client):
        incident_id, _ = _build_simple_incident(_tag("HistApiDB"), _tag("HistApiSvc"))

        r = client.get(f"/api/v1/incidents/{incident_id}/history")
        assert r.status_code == 200
        body = r.json()
        for key in (
            "incident", "events", "entities", "topology", "correlation_signals", "rules_triggered",
            "root_cause_candidates", "operator_confirmation", "resolution", "resolution_time",
            "business_impact", "affected_services", "affected_apis", "affected_databases", "timeline",
        ):
            assert key in body

        r = client.get("/api/v1/incidents/999999999/history")
        assert r.status_code == 404


def _build_simple_incident(db_name: str, svc_name: str) -> tuple[int, int]:
    """A minimal, real, correlated incident (database depends_on service ->
    correlated -> formed into an incident). Returns (incident_id, database_entity_id).
    """
    db = SessionLocal()
    try:
        database = _entity(db, EntityType.database, db_name)
        service = _entity(db, EntityType.service, svc_name)
        record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                             from_entity_id=service, to_entity_id=database)
        tag = _tag("SIMPLE_DB")
        create_rule(db, rule_id=f"SIMPLE-{tag}", name="simple incident helper",
                    conditions=[{"event_type": tag}, {"relationship": "depends_on"}, {"time_window_seconds": 900}])
        ev_db = _event(db, entity_id=database, problem_type=tag, last_seen=NOW)
        ev_svc = _event(db, entity_id=service, problem_type=_tag("SIMPLE_SVC"), last_seen=NOW + timedelta(seconds=5))
        db.commit()
        outcome = correlate_pair(db, ev_db.id, ev_svc.id)
        assert outcome.correlated is True
        db.commit()
        incident = form_or_update_incident_from_correlation(db, db.get(Correlation, outcome.correlation_id))
        db.commit()
        return incident.id, database
    finally:
        db.close()
