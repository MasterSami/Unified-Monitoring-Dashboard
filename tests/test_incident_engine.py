"""Correlation Phase 5: application architecture correlation + incidents.

Covers app.application_health (API-level rollup, never "one API down = app
down"), app.trace_ingest (distributed trace ingest -> topology + canonical
events), and app.incident_engine (root-cause candidates, timeline, symptom
classification, merge/split). No AI/ML — root cause ranking is a
deterministic graph question over Phase 3's EntityRelationship table plus
Phase 4's stored CorrelationEvidence, never a learned score.

CorrelationRule rows are GLOBAL and shared across the whole test session —
see tests/test_correlation_engine.py's own note. Every rule created below is
scoped the same way, with a unique event_type tag.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.application_health import compute_application_health
from app.correlation_engine import correlate_pair
from app.correlation_rules import create_rule
from app.db import SessionLocal
from app.dependency_graph import record_relationship
from app.entity_resolution import resolve_named_entity
from app.incident_engine import (
    build_timeline,
    form_or_update_incident_from_correlation,
    merge_incidents,
    root_cause_candidates,
    run_incident_formation,
    split_incident,
)
from app.models import (
    Alert,
    CanonicalEntity,
    Correlation,
    EntityType,
    Incident,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    SourcePlatform,
    SymptomRole,
    TopologySource,
)
from app.trace_ingest import ingest_trace_spans
from tests.conftest import RUNBOOK_PASSWORD, RUNBOOK_USER

NOW = datetime(2026, 9, 19, 10, 1, 0, tzinfo=timezone.utc)

_tag_counter = itertools.count(1)


def _tag(label: str) -> str:
    return f"{label}_{next(_tag_counter)}"


def _entity(db, entity_type: EntityType, name: str) -> int:
    e = CanonicalEntity(entity_type=entity_type, canonical_name=name)
    db.add(e)
    db.flush()
    return e.id


def _event(
    db, *, entity_id: int | None, problem_type: str, last_seen: datetime,
    status: LogicalEventStatus = LogicalEventStatus.open,
) -> LogicalEvent:
    fp = f"entity:{entity_id}|problem:{problem_type}|__test__:{id(object())}"
    le = LogicalEvent(
        fingerprint=fp, entity_id=entity_id, normalized_problem_type=problem_type,
        status=status, occurrence_count=1, first_seen=last_seen, last_seen=last_seen,
        sources=[], title=problem_type, current_severity_int=3, current_severity_label="Average",
    )
    db.add(le)
    db.flush()
    return le


def _occurrence(
    db, *, logical_event: LogicalEvent, platform: SourcePlatform = SourcePlatform.zabbix,
    instance: str = "T1", external_id: str | None = None, business_service: str | None = None,
) -> Alert:
    a = Alert(
        source_platform=platform, source_instance=instance,
        external_id=external_id or f"occ-{id(object())}",
        severity_int=3, severity_label="Average", title=logical_event.normalized_problem_type,
        started_at=logical_event.last_seen, resolved=False,
        entity_id=logical_event.entity_id, logical_event_id=logical_event.id,
        business_service=business_service,
    )
    db.add(a)
    db.flush()
    return a


def _correlate_with_rule(db, ev_a, ev_b, *, signals: list[str] | None = None, relationship: str | None = None):
    """Create a rule scoped to ``ev_a``'s own (already-unique, via _tag)
    problem_type — same test-isolation pattern as test_correlation_engine.py
    — and correlate the pair.
    """
    conditions = [{"event_type": ev_a.normalized_problem_type}, {"time_window_seconds": 900}]
    if signals:
        conditions.append({"signals": signals})
    if relationship:
        conditions.append({"relationship": relationship})
    create_rule(db, rule_id=f"IE-{ev_a.normalized_problem_type}", name="incident engine test rule", conditions=conditions)
    return correlate_pair(db, ev_a.id, ev_b.id)


class TestApplicationHealth:
    def test_one_failing_api_out_of_three_is_degraded_not_down(self, client):
        db = SessionLocal()
        try:
            app_id = _entity(db, EntityType.application, "CustomerApp")
            get_api = _entity(db, EntityType.api, "GET /customer")
            update_api = _entity(db, EntityType.api, "POST /customer/update")
            delete_api = _entity(db, EntityType.api, "DELETE /customer")
            for api_id in (get_api, update_api, delete_api):
                record_relationship(
                    db, source=TopologySource.dynatrace, relationship_type=RelationshipType.provides,
                    from_entity_id=app_id, to_entity_id=api_id,
                )
            _event(db, entity_id=update_api, problem_type=_tag("APIFAIL"), last_seen=NOW)
            db.commit()

            health = compute_application_health(db, app_id)
            assert health.status == "degraded"
            assert health.total_apis == 3
            assert [a["entity_id"] for a in health.affected_apis] == [update_api]
            assert {h["entity_id"] for h in health.healthy_apis} == {get_api, delete_api}
        finally:
            db.close()

    def test_no_failing_apis_is_healthy(self, client):
        db = SessionLocal()
        try:
            app_id = _entity(db, EntityType.application, "QuietApp")
            api_id = _entity(db, EntityType.api, "GET /quiet")
            record_relationship(
                db, source=TopologySource.dynatrace, relationship_type=RelationshipType.provides,
                from_entity_id=app_id, to_entity_id=api_id,
            )
            db.commit()
            health = compute_application_health(db, app_id)
            assert health.status == "healthy"
        finally:
            db.close()

    def test_every_known_api_failing_is_down(self, client):
        db = SessionLocal()
        try:
            app_id = _entity(db, EntityType.application, "DoomedApp")
            api_id = _entity(db, EntityType.api, "GET /doomed")
            record_relationship(
                db, source=TopologySource.dynatrace, relationship_type=RelationshipType.provides,
                from_entity_id=app_id, to_entity_id=api_id,
            )
            _event(db, entity_id=api_id, problem_type=_tag("DOOM"), last_seen=NOW)
            db.commit()
            health = compute_application_health(db, app_id)
            assert health.status == "down"
        finally:
            db.close()

    def test_unknown_or_non_application_entity_returns_none(self, client):
        db = SessionLocal()
        try:
            host_id = _entity(db, EntityType.host, "SomeHost")
            db.commit()
            assert compute_application_health(db, host_id) is None
            assert compute_application_health(db, 10**9) is None
        finally:
            db.close()


class TestTraceIngest:
    def test_error_span_becomes_a_canonical_event_healthy_spans_do_not(self, client):
        db = SessionLocal()
        try:
            trace_id = _tag("TRACE")
            result = ingest_trace_spans(db, instance="Dynatrace-1", spans=[
                {"trace_id": trace_id, "span_id": "S1", "service": "OrderSvc",
                 "api": "GET /order", "http_method": "GET", "status_code": 200,
                 "started_at": NOW},
                {"trace_id": trace_id, "span_id": "S2", "service": "OrderSvc",
                 "api": "POST /order/submit", "http_method": "POST", "status_code": 500,
                 "error": True, "duration_ms": 812.0, "started_at": NOW},
            ])
            db.commit()
            assert result.received == 2
            assert result.inserted == 1

            row = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S2"))
            assert row is not None
            assert row.is_error is True
            assert row.http_status_code == 500
            assert row.duration_ms == 812.0
            assert row.trace_id == trace_id
            assert row.api_name == "POST /order/submit"

            healthy_row = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S1"))
            assert healthy_row is None
        finally:
            db.close()

    def test_ingest_declares_provides_and_depends_on_topology(self, client):
        db = SessionLocal()
        try:
            trace_id = _tag("TOPOTRACE")
            ingest_trace_spans(db, instance="Dynatrace-1", spans=[
                {"trace_id": trace_id, "span_id": "S1", "service": "PaySvc",
                 "api": "POST /pay", "error": True, "started_at": NOW},
                {"trace_id": trace_id, "span_id": "S2", "service": "PaySvc",
                 "database": "PayDB", "parent_span_id": "S1", "error": True, "started_at": NOW},
            ])
            db.commit()

            svc = db.scalar(select(CanonicalEntity).where(
                CanonicalEntity.entity_type == EntityType.service, CanonicalEntity.canonical_name == "PaySvc",
            ))
            api = db.scalar(select(CanonicalEntity).where(
                CanonicalEntity.entity_type == EntityType.api, CanonicalEntity.canonical_name == "POST /pay",
            ))
            database = db.scalar(select(CanonicalEntity).where(
                CanonicalEntity.entity_type == EntityType.database, CanonicalEntity.canonical_name == "PayDB",
            ))
            assert svc and api and database

            from app.models import EntityRelationship
            provides_edge = db.scalar(select(EntityRelationship).where(
                EntityRelationship.from_entity_id == svc.id, EntityRelationship.to_entity_id == api.id,
                EntityRelationship.relationship_type == RelationshipType.provides,
            ))
            depends_edge = db.scalar(select(EntityRelationship).where(
                EntityRelationship.from_entity_id == svc.id, EntityRelationship.to_entity_id == database.id,
                EntityRelationship.relationship_type == RelationshipType.depends_on,
            ))
            assert provides_edge is not None
            assert depends_edge is not None

            api_row = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S1"))
            assert api_row.db_calls == ["PayDB"]
        finally:
            db.close()

    def test_explicit_resolved_span_recovers_the_prior_occurrence(self, client):
        db = SessionLocal()
        try:
            trace_id = _tag("RECOVER")
            ingest_trace_spans(db, instance="Dynatrace-1", spans=[
                {"trace_id": trace_id, "span_id": "S1", "service": "FlakySvc",
                 "api": "GET /flaky", "error": True, "started_at": NOW},
            ])
            db.commit()
            row = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S1"))
            assert row.resolved is False

            ingest_trace_spans(db, instance="Dynatrace-1", spans=[
                {"trace_id": trace_id, "span_id": "S1", "resolved": True},
            ])
            db.commit()
            db.refresh(row)
            assert row.resolved is True
            assert row.resolved_at is not None
        finally:
            db.close()


class TestRootCauseCandidates:
    def test_database_is_the_sole_root_cause_of_service_and_api_failures(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, "OrdersDB")
            service = _entity(db, EntityType.service, "OrdersSvc")
            api = _entity(db, EntityType.api, "POST /orders")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=service, to_entity_id=database)
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.provides,
                                 from_entity_id=service, to_entity_id=api)

            ev_db = _event(db, entity_id=database, problem_type=_tag("DB_TIMEOUT"), last_seen=NOW)
            ev_api = _event(db, entity_id=api, problem_type=_tag("API_500"), last_seen=NOW + timedelta(seconds=8))
            db.commit()

            candidates = root_cause_candidates(db, [ev_db, ev_api])
            assert [c["entity_id"] for c in candidates] == [database]
            assert candidates[0]["evidence"]  # never a bare, unexplained claim
        finally:
            db.close()

    def test_two_unrelated_sinks_both_come_back_as_candidates(self, client):
        """No dependency edge between them -> genuine conflict, both surface
        (task section 14: show all candidates with evidence, never fabricate
        a single answer)."""
        db = SessionLocal()
        try:
            db_entity = _entity(db, EntityType.database, "ConflictDB")
            gw_entity = _entity(db, EntityType.network_device, "ConflictGateway")
            ev_a = _event(db, entity_id=db_entity, problem_type=_tag("CONFLICT_A"), last_seen=NOW)
            ev_b = _event(db, entity_id=gw_entity, problem_type=_tag("CONFLICT_B"), last_seen=NOW)
            db.commit()

            candidates = root_cause_candidates(db, [ev_a, ev_b])
            assert {c["entity_id"] for c in candidates} == {db_entity, gw_entity}
        finally:
            db.close()

    def test_network_switch_down_makes_it_the_root_cause_for_every_downstream_host(self, client):
        """Task section 6: CORE-SW-01 down, APP01/APP02/DB01 downstream ->
        one root cause candidate, three affected."""
        db = SessionLocal()
        try:
            switch = _entity(db, EntityType.network_device, "CORE-SW-01")
            app1 = _entity(db, EntityType.host, "APP01")
            app2 = _entity(db, EntityType.host, "APP02")
            db1 = _entity(db, EntityType.host, "DB01")
            # connects_to is a DEPENDENCY_TYPES edge ("from needs to" — to's
            # failure propagates back to from), so each HOST connects_to the
            # SWITCH (the host needs it to be reachable), not the other way
            # round — same direction convention as depends_on/calls/uses.
            for host in (app1, app2, db1):
                record_relationship(db, source=TopologySource.monitoring, relationship_type=RelationshipType.connects_to,
                                     from_entity_id=host, to_entity_id=switch)

            ev_switch = _event(db, entity_id=switch, problem_type=_tag("SWITCH_DOWN"), last_seen=NOW)
            ev_app1 = _event(db, entity_id=app1, problem_type=_tag("APP1_UNREACH"), last_seen=NOW + timedelta(seconds=5))
            ev_app2 = _event(db, entity_id=app2, problem_type=_tag("APP2_UNREACH"), last_seen=NOW + timedelta(seconds=6))
            ev_db1 = _event(db, entity_id=db1, problem_type=_tag("DB1_UNREACH"), last_seen=NOW + timedelta(seconds=7))
            db.commit()

            candidates = root_cause_candidates(db, [ev_switch, ev_app1, ev_app2, ev_db1])
            assert [c["entity_id"] for c in candidates] == [switch]
            evidence_text = " ".join(candidates[0]["evidence"])
            assert "APP01" in evidence_text and "APP02" in evidence_text and "DB01" in evidence_text
        finally:
            db.close()


class TestIncidentFormation:
    def test_incident_formed_from_a_correlation_has_severity_status_and_affected(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, "FormDB")
            service = _entity(db, EntityType.service, "FormSvc")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=service, to_entity_id=database)
            ev_db = _event(db, entity_id=database, problem_type=_tag("FORM_DB"), last_seen=NOW)
            ev_svc = _event(db, entity_id=service, problem_type=_tag("FORM_SVC"), last_seen=NOW + timedelta(seconds=10))
            db.commit()

            outcome = _correlate_with_rule(db, ev_db, ev_svc, relationship="depends_on")
            assert outcome.correlated is True
            db.commit()

            correlation = db.get(Correlation, outcome.correlation_id)
            incident = form_or_update_incident_from_correlation(db, correlation)
            db.commit()

            assert incident is not None
            assert incident.status == "open"
            assert incident.severity_int == 3
            assert set(incident.related_events) == {ev_db.id, ev_svc.id}
            assert incident.root_cause_candidates[0]["entity_id"] == database
            assert incident.member_roles[str(ev_db.id)] == SymptomRole.primary.value
            assert incident.member_roles[str(ev_svc.id)] == SymptomRole.related_symptom.value
            assert incident.affected["services"] and incident.affected["services"][0]["entity_id"] == service
        finally:
            db.close()

    def test_a_third_correlated_event_updates_the_same_incident(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, "GrowDB")
            svc_a = _entity(db, EntityType.service, "GrowSvcA")
            svc_b = _entity(db, EntityType.service, "GrowSvcB")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc_a, to_entity_id=database)
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc_b, to_entity_id=database)

            ev_db = _event(db, entity_id=database, problem_type=_tag("GROW_DB"), last_seen=NOW)
            ev_a = _event(db, entity_id=svc_a, problem_type=_tag("GROW_A"), last_seen=NOW + timedelta(seconds=5))
            ev_b = _event(db, entity_id=svc_b, problem_type=_tag("GROW_B"), last_seen=NOW + timedelta(seconds=6))
            db.commit()

            out1 = _correlate_with_rule(db, ev_db, ev_a, relationship="depends_on")
            db.commit()
            incident1 = form_or_update_incident_from_correlation(db, db.get(Correlation, out1.correlation_id))
            db.commit()
            first_id = incident1.id

            # The FIRST rule already matches this pair too (same event_type,
            # ev_db.normalized_problem_type, on either side) — no second rule
            # needed; ev_db is common to both pairs.
            out2 = correlate_pair(db, ev_db.id, ev_b.id)
            assert out2.correlated is True
            assert out2.correlation_id == out1.correlation_id
            db.commit()

            incident2 = form_or_update_incident_from_correlation(db, db.get(Correlation, out2.correlation_id))
            db.commit()
            assert incident2.id == first_id
            assert incident2.status == "updated"
            assert set(incident2.related_events) == {ev_db.id, ev_a.id, ev_b.id}
        finally:
            db.close()

    def test_resolving_every_member_marks_the_incident_resolved(self, client):
        db = SessionLocal()
        try:
            svc = _entity(db, EntityType.service, "ResolveSvc")
            db_entity = _entity(db, EntityType.database, "ResolveDB")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc, to_entity_id=db_entity)
            ev_db = _event(db, entity_id=db_entity, problem_type=_tag("RESOLVE_DB"), last_seen=NOW,
                            status=LogicalEventStatus.resolved)
            ev_svc = _event(db, entity_id=svc, problem_type=_tag("RESOLVE_SVC"), last_seen=NOW + timedelta(seconds=5),
                             status=LogicalEventStatus.resolved)
            db.commit()

            outcome = _correlate_with_rule(db, ev_db, ev_svc, relationship="depends_on")
            db.commit()
            incident = form_or_update_incident_from_correlation(db, db.get(Correlation, outcome.correlation_id))
            db.commit()
            assert incident.status == "resolved"
        finally:
            db.close()

    def test_run_incident_formation_processes_existing_correlations(self, client):
        db = SessionLocal()
        try:
            svc = _entity(db, EntityType.service, "BatchSvc")
            db_entity = _entity(db, EntityType.database, "BatchDB")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=svc, to_entity_id=db_entity)
            ev_db = _event(db, entity_id=db_entity, problem_type=_tag("BATCH_DB"), last_seen=NOW)
            ev_svc = _event(db, entity_id=svc, problem_type=_tag("BATCH_SVC"), last_seen=NOW + timedelta(seconds=5))
            db.commit()
            outcome = _correlate_with_rule(db, ev_db, ev_svc, relationship="depends_on")
            db.commit()

            result = run_incident_formation(db, [outcome.correlation_id])
            db.commit()
            assert result["incidents_formed"] == 1
        finally:
            db.close()


class TestTimeline:
    def test_timeline_is_chronological_and_includes_recovery(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, "TimelineDB")
            service = _entity(db, EntityType.service, "TimelineSvc")
            ev_db = _event(db, entity_id=database, problem_type=_tag("TL_DB"), last_seen=NOW)
            ev_svc = _event(db, entity_id=service, problem_type=_tag("TL_SVC"), last_seen=NOW + timedelta(seconds=4))
            db_occ = _occurrence(db, logical_event=ev_db)
            db_occ.started_at = NOW
            db_occ.resolved = True
            db_occ.resolved_at = NOW + timedelta(seconds=15)
            svc_occ = _occurrence(db, logical_event=ev_svc)
            svc_occ.started_at = NOW + timedelta(seconds=4)
            db.commit()

            timeline = build_timeline(db, [ev_db, ev_svc])
            kinds = [(e["kind"], e["entity_name"]) for e in timeline]
            assert kinds == [
                ("onset", "TimelineDB"), ("onset", "TimelineSvc"), ("recovery", "TimelineDB"),
            ]
            timestamps = [e["timestamp"] for e in timeline]
            assert timestamps == sorted(timestamps)
        finally:
            db.close()


class TestMergeAndSplit:
    def test_merge_combines_membership_into_the_lower_id_survivor(self, client):
        db = SessionLocal()
        try:
            ev1 = _event(db, entity_id=_entity(db, EntityType.service, "MergeSvc1"),
                          problem_type=_tag("MERGE1"), last_seen=NOW)
            ev2 = _event(db, entity_id=_entity(db, EntityType.service, "MergeSvc2"),
                          problem_type=_tag("MERGE2"), last_seen=NOW)
            ev3 = _event(db, entity_id=_entity(db, EntityType.service, "MergeSvc3"),
                          problem_type=_tag("MERGE3"), last_seen=NOW)
            ev4 = _event(db, entity_id=_entity(db, EntityType.service, "MergeSvc4"),
                          problem_type=_tag("MERGE4"), last_seen=NOW)
            db.commit()

            inc_a = Incident(related_events=[ev1.id, ev2.id])
            inc_b = Incident(related_events=[ev3.id, ev4.id])
            db.add_all([inc_a, inc_b])
            db.flush()
            from app.incident_engine import recompute_incident
            recompute_incident(db, inc_a, is_new=True)
            recompute_incident(db, inc_b, is_new=True)
            db.commit()
            a_id, b_id = inc_a.id, inc_b.id

            survivor = merge_incidents(db, [b_id, a_id])
            db.commit()
            assert survivor.id == min(a_id, b_id)
            assert set(survivor.related_events) == {ev1.id, ev2.id, ev3.id, ev4.id}

            other_id = b_id if survivor.id == a_id else a_id
            other = db.get(Incident, other_id)
            assert other.status == "merged"
            assert other.merged_into_id == survivor.id
            # never deleted, per "never delete symptoms"
            assert other.related_events == ([ev3.id, ev4.id] if other_id == b_id else [ev1.id, ev2.id])
        finally:
            db.close()

    def test_split_carves_events_out_without_deleting_them_from_the_original(self, client):
        db = SessionLocal()
        try:
            ev1 = _event(db, entity_id=_entity(db, EntityType.service, "SplitSvc1"),
                          problem_type=_tag("SPLIT1"), last_seen=NOW)
            ev2 = _event(db, entity_id=_entity(db, EntityType.service, "SplitSvc2"),
                          problem_type=_tag("SPLIT2"), last_seen=NOW)
            ev3 = _event(db, entity_id=_entity(db, EntityType.service, "SplitSvc3"),
                          problem_type=_tag("SPLIT3"), last_seen=NOW)
            db.commit()

            original = Incident(related_events=[ev1.id, ev2.id, ev3.id])
            db.add(original)
            db.flush()
            from app.incident_engine import recompute_incident
            recompute_incident(db, original, is_new=True)
            db.commit()

            new_incident = split_incident(db, original.id, [ev3.id])
            db.commit()

            db.refresh(original)
            assert set(original.related_events) == {ev1.id, ev2.id}
            assert set(new_incident.related_events) == {ev3.id}
            assert new_incident.id != original.id
        finally:
            db.close()


class TestIncidentAPI:
    def test_full_lifecycle_through_the_api(self, client):
        db = SessionLocal()
        try:
            database = _entity(db, EntityType.database, "APIDB")
            service = _entity(db, EntityType.service, "APISvc")
            record_relationship(db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                                 from_entity_id=service, to_entity_id=database)
            ev_db = _event(db, entity_id=database, problem_type=_tag("API_DB"), last_seen=NOW)
            ev_svc = _event(db, entity_id=service, problem_type=_tag("API_SVC"), last_seen=NOW + timedelta(seconds=5))
            _occurrence(db, logical_event=ev_db)
            _occurrence(db, logical_event=ev_svc)
            db.commit()
            outcome = _correlate_with_rule(db, ev_db, ev_svc, relationship="depends_on")
            db.commit()
        finally:
            db.close()

        r = client.post("/api/v1/incidents/run", params={"correlation_id": outcome.correlation_id})
        assert r.status_code == 200
        assert r.json()["incidents_formed"] == 1

        r = client.get("/api/v1/incidents", params={"event_id": ev_db.id})
        assert r.status_code == 200
        incidents = r.json()
        assert len(incidents) == 1
        incident_id = incidents[0]["id"]
        assert incidents[0]["root_cause_candidates"][0]["canonical_name"] == "APIDB"

        r = client.get(f"/api/v1/incidents/{incident_id}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["timeline"]
        assert detail["evidence"]

        r = client.get(f"/api/v1/incidents/{incident_id}/timeline")
        assert r.status_code == 200
        assert len(r.json()) >= 2

        r = client.get(f"/api/v1/incidents/{incident_id}/evidence")
        assert r.status_code == 200
        assert r.json()

        r = client.get(f"/api/v1/incidents/{incident_id}/impact")
        assert r.status_code == 200
        assert r.json()["affected"]["services"]

        r = client.get(f"/api/v1/incidents/{incident_id}/correlation-graph")
        assert r.status_code == 200
        graph = r.json()
        assert any(n["role"] == "root_cause_candidate" for n in graph["nodes"])

        r = client.get("/api/v1/incidents/999999999")
        assert r.status_code == 404

    def test_split_then_merge_via_api(self, client):
        db = SessionLocal()
        try:
            ev1 = _event(db, entity_id=_entity(db, EntityType.service, "APISplitSvc1"),
                          problem_type=_tag("APISPLIT1"), last_seen=NOW)
            ev2 = _event(db, entity_id=_entity(db, EntityType.service, "APISplitSvc2"),
                          problem_type=_tag("APISPLIT2"), last_seen=NOW)
            db.commit()
            incident = Incident(related_events=[ev1.id, ev2.id])
            db.add(incident)
            db.flush()
            from app.incident_engine import recompute_incident
            recompute_incident(db, incident, is_new=True)
            db.commit()
            incident_id = incident.id
        finally:
            db.close()

        client.cookies.clear()
        # Merge/split require the Runbook operator login (Correlation
        # Phase 6's only authorization) — unauthenticated is rejected first.
        r = client.post(f"/api/v1/incidents/{incident_id}/split", json={"event_ids": [ev2.id]})
        assert r.status_code == 401

        client.post(
            "/runbook/login", data={"username": RUNBOOK_USER, "password": RUNBOOK_PASSWORD},
            follow_redirects=True,
        )

        r = client.post(f"/api/v1/incidents/{incident_id}/split", json={"event_ids": [ev2.id]})
        assert r.status_code == 200
        new_id = r.json()["id"]
        assert new_id != incident_id

        r = client.post("/api/v1/incidents/merge", json={"incident_ids": [incident_id, new_id]})
        assert r.status_code == 200
        assert set(r.json()["related_events"]) == {ev1.id, ev2.id}

        r = client.post("/api/v1/incidents/merge", json={"incident_ids": [incident_id]})
        assert r.status_code == 422

        client.cookies.clear()

    def test_traces_endpoint_ingests_and_application_health_endpoint_reads_it(self, client):
        trace_id = _tag("APITRACE")
        r = client.post("/api/v1/traces", json={
            "source_instance": "Dynatrace-API-Test",
            "spans": [
                {"trace_id": trace_id, "span_id": "S1", "application": "APITraceApp",
                 "service": "APITraceSvc", "api": "POST /apitrace", "status_code": 500,
                 "error": True},
            ],
        })
        assert r.status_code == 201
        body = r.json()
        assert body["inserted"] == 1
        assert body["relationships_declared"] >= 1

        db = SessionLocal()
        try:
            app_entity = db.scalar(select(CanonicalEntity).where(
                CanonicalEntity.entity_type == EntityType.application,
                CanonicalEntity.canonical_name == "APITraceApp",
            ))
        finally:
            db.close()
        assert app_entity is not None

        r = client.get(f"/api/v1/applications/{app_entity.id}/health")
        assert r.status_code == 200
        # Only one known API under this application, and it's failing —
        # 100% of its known APIs are down, so "down", not "degraded" (that
        # needs at least one OTHER known, still-healthy API — see
        # TestApplicationHealth.test_one_failing_api_out_of_three...).
        assert r.json()["status"] == "down"

        r = client.get("/api/v1/applications/999999999/health")
        assert r.status_code == 404


class TestAcceptanceCriteria:
    def test_customer_db_failure_correlates_into_one_incident_with_full_context(self, client):
        """Task's own acceptance test: POST /customer/update HTTP 500, a
        Dynatrace trace showing CustomerService -> CustomerDB -> DB timeout,
        and Zabbix separately reporting a CustomerDB connection failure, must
        all become ONE correlated incident, root cause candidate CustomerDB,
        affecting CustomerService / POST /customer/update / Customer360.
        """
        trace_id = _tag("ACCEPT_TRACE")
        r = client.post("/api/v1/traces", json={
            "source_instance": "Dynatrace-Prod",
            "spans": [
                {
                    "trace_id": trace_id, "span_id": "S1", "service": "CustomerService",
                    "api": "POST /customer/update", "http_method": "POST", "status_code": 500,
                    "error": True, "business_transaction": "Update Customer",
                    "business_service": "Customer360", "started_at": NOW.isoformat(),
                },
                {
                    "trace_id": trace_id, "span_id": "S2", "service": "CustomerService",
                    "database": "CustomerDB", "parent_span_id": "S1", "error": True,
                    "business_service": "Customer360", "started_at": NOW.isoformat(),
                },
            ],
        })
        assert r.status_code == 201

        db = SessionLocal()
        try:
            ev_api = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S1")).logical_event_id
            ev_db_trace = db.scalar(select(Alert).where(Alert.external_id == f"{trace_id}:S2")).logical_event_id
            ev_api_obj = db.get(LogicalEvent, ev_api)
            ev_db_trace_obj = db.get(LogicalEvent, ev_db_trace)

            db_entity = db.scalar(select(CanonicalEntity).where(
                CanonicalEntity.entity_type == EntityType.database,
                CanonicalEntity.canonical_name == "CustomerDB",
            ))
            # Zabbix independently reports the SAME CustomerDB entity failing
            # (Phase 1 entity resolution already ties differently-sourced
            # identifiers to one canonical entity — assumed proven, not
            # re-tested here).
            zabbix_event = _event(
                db, entity_id=db_entity.id, problem_type=_tag("ZBX_DB_CONN_FAIL"),
                last_seen=NOW + timedelta(seconds=3),
            )
            _occurrence(db, logical_event=zabbix_event, business_service="Customer360")
            db.commit()

            # Scoped by these events' own (already-unique, since
            # normalize_problem_type ran on a real trace-ingested title) tags
            # — same cross-test-pollution guard as _correlate_with_rule.
            create_rule(
                db, rule_id=f"ACCEPT-TRACE-{ev_api_obj.normalized_problem_type}", name="exact trace",
                conditions=[
                    {"event_type": ev_api_obj.normalized_problem_type},
                    {"signals": ["same_trace"]}, {"time_window_seconds": 900},
                ],
            )
            out1 = correlate_pair(db, ev_api, ev_db_trace)
            assert out1.correlated is True
            assert out1.correlation_type.value == "trace"
            db.commit()

            create_rule(
                db, rule_id=f"ACCEPT-DB-{ev_db_trace_obj.normalized_problem_type}", name="same database",
                conditions=[
                    {"event_type": ev_db_trace_obj.normalized_problem_type},
                    {"signals": ["same_database"]}, {"time_window_seconds": 900},
                ],
            )
            out2 = correlate_pair(db, ev_db_trace, zabbix_event.id)
            assert out2.correlated is True
            assert out2.correlation_id == out1.correlation_id
            db.commit()

            correlation = db.get(Correlation, out1.correlation_id)
            assert set(correlation.member_event_ids) == {ev_api, ev_db_trace, zabbix_event.id}

            incident = form_or_update_incident_from_correlation(db, correlation)
            db.commit()

            assert incident is not None
            assert set(incident.related_events) == {ev_api, ev_db_trace, zabbix_event.id}
            assert incident.business_service == "Customer360"
            assert len(incident.root_cause_candidates) == 1
            assert incident.root_cause_candidates[0]["canonical_name"] == "CustomerDB"
            assert incident.root_cause_candidates[0]["evidence"]

            affected_names = {a["canonical_name"] for a in incident.affected["services"]}
            assert "CustomerService" in affected_names
            affected_apis = {a["canonical_name"] for a in incident.affected["apis"]}
            assert "POST /customer/update" in affected_apis

            evidence_count = len(db.scalars(
                select(Correlation).where(Correlation.id.in_(incident.source_correlation_ids))
            ).all())
            assert evidence_count >= 1
        finally:
            db.close()

        r = client.get("/api/v1/incidents", params={"event_id": ev_api})
        assert r.status_code == 200
        assert len(r.json()) == 1
        body = r.json()[0]
        assert body["root_cause_candidates"][0]["canonical_name"] == "CustomerDB"
        assert body["business_service"] == "Customer360"

        r = client.get(f"/api/v1/incidents/{body['id']}/evidence")
        assert r.status_code == 200
        assert len(r.json()) > 0
