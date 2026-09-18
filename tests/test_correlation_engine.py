"""Correlation Phase 4: deterministic correlation rule engine.

Covers app.correlation_rules (rule validation/priority/weights) and
app.correlation_engine (signal computation, scoring, rule matching,
false-correlation protection, lifecycle). No AI/ML — every decision is a
fixed boolean check, scored with configurable weights, gated by
configuration-driven rules.

Rules are GLOBAL (not scoped per test, per instance, or per anything) —
exactly like production, where a rule applies to every future pair that
matches it. Every rule created below is therefore scoped with a unique
``event_type`` condition tied to a test-local tag (see ``_tag()``), so one
test's rule can never accidentally fire against another test's pair sharing
the same DB. This mirrors real rule design (a targeted rule names the event
type it's for) as much as it protects test isolation.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.correlation_engine import correlate_pair
from app.correlation_rules import create_rule
from app.db import SessionLocal
from app.dependency_graph import record_relationship
from app.models import (
    Alert,
    CanonicalEntity,
    Correlation,
    CorrelationRule,
    CorrelationStatus,
    EntityType,
    HostStatus,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    SourcePlatform,
    TopologySource,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

_tag_counter = itertools.count(1)


def _tag(label: str) -> str:
    """A problem-type string unique to this call, so a rule scoped to it can
    never match a different test's events."""
    return f"{label}_{next(_tag_counter)}"


def _entity(db, entity_type: EntityType, name: str) -> int:
    e = CanonicalEntity(entity_type=entity_type, canonical_name=name)
    db.add(e)
    db.flush()
    return e.id


def _event(
    db, *, entity_id: int | None, problem_type: str, last_seen: datetime,
    status: LogicalEventStatus = LogicalEventStatus.open, occurrence_count: int = 1,
) -> LogicalEvent:
    fp = f"entity:{entity_id}|problem:{problem_type}|__test__:{id(object())}"
    le = LogicalEvent(
        fingerprint=fp, entity_id=entity_id, normalized_problem_type=problem_type,
        status=status, occurrence_count=occurrence_count, first_seen=last_seen,
        last_seen=last_seen, sources=[], title=problem_type,
        current_severity_int=3, current_severity_label="Average",
    )
    db.add(le)
    db.flush()
    return le


def _occurrence(
    db, *, logical_event: LogicalEvent, platform: SourcePlatform, instance: str,
    external_id: str, trace_id: str | None = None, business_service: str | None = None,
    resolved: bool = False,
) -> Alert:
    a = Alert(
        source_platform=platform, source_instance=instance, external_id=external_id,
        severity_int=3, severity_label="Average", title=logical_event.normalized_problem_type,
        started_at=logical_event.last_seen, resolved=resolved,
        entity_id=logical_event.entity_id, logical_event_id=logical_event.id,
        trace_id=trace_id, business_service=business_service,
    )
    db.add(a)
    db.flush()
    return a


class TestRuleValidation:
    def test_a_rule_needs_a_time_window(self, client):
        db = SessionLocal()
        try:
            with pytest.raises(ValueError, match="time_window_seconds"):
                create_rule(
                    db, rule_id="BAD-001", name="no window",
                    conditions=[{"signals": ["same_entity"]}],
                )
        finally:
            db.close()

    def test_a_rule_needs_an_allowed_window(self, client):
        db = SessionLocal()
        try:
            with pytest.raises(ValueError, match="60, 120, 300"):
                create_rule(
                    db, rule_id="BAD-002", name="bad window",
                    conditions=[{"signals": ["same_entity"]}, {"time_window_seconds": 47}],
                )
        finally:
            db.close()

    def test_weak_signals_alone_are_rejected(self, client):
        """False-correlation protection, enforced at creation time: temporal
        + same_host + same_application + multi_source, however combined,
        can never be a valid rule on their own."""
        db = SessionLocal()
        try:
            with pytest.raises(ValueError, match="never sufficient"):
                create_rule(
                    db, rule_id="BAD-003", name="weak only",
                    conditions=[
                        {"signals": ["same_host", "same_application", "multi_source"]},
                        {"time_window_seconds": 300},
                    ],
                )
        finally:
            db.close()

    def test_a_rule_with_one_strong_signal_is_accepted(self, client):
        db = SessionLocal()
        try:
            rule = create_rule(
                db, rule_id="OK-001", name="entity ok",
                conditions=[{"event_type": _tag("VALIDATE")}, {"signals": ["same_entity"]},
                            {"time_window_seconds": 300}],
            )
            assert rule.priority_tier == 4
        finally:
            db.close()


class TestFalseCorrelationProtection:
    """Section 8: never correlate solely because of same timestamp, same
    host, or same application."""

    def test_two_unrelated_application_failures_at_the_same_time_do_not_correlate(self, client):
        db = SessionLocal()
        try:
            t = _tag("APPFAIL")
            create_rule(
                db, rule_id=f"ENTITY-RULE-{t}", name="entity",
                conditions=[{"event_type": t}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )
            app_a = _entity(db, EntityType.application, "AppAlpha")
            app_b = _entity(db, EntityType.application, "AppBeta")
            ev_a = _event(db, entity_id=app_a, problem_type=t, last_seen=NOW)
            ev_b = _event(db, entity_id=app_b, problem_type=t, last_seen=NOW)
            db.commit()

            outcome = correlate_pair(db, ev_a.id, ev_b.id)
            assert outcome.correlated is False
            assert "NOT_CORRELATED" in outcome.reason
        finally:
            db.close()

    def test_same_host_different_services_unrelated_problems_do_not_correlate(self, client):
        """Two different services HOSTED_ON the same host, unrelated
        problems — same_host is real but explicitly insufficient alone."""
        db = SessionLocal()
        try:
            t_a, t_b = _tag("HOSTSHARE_A"), _tag("HOSTSHARE_B")
            # A control rule scoped elsewhere, to prove a *matching* rule
            # existing in the system doesn't help when the signal is weak.
            create_rule(
                db, rule_id=f"CTRL-{t_a}", name="entity for control",
                conditions=[{"event_type": "SOME_OTHER_TAG_NEVER_USED"},
                            {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )
            host = _entity(db, EntityType.host, "SharedHost1")
            svc_a = _entity(db, EntityType.service, "SvcOnHostA")
            svc_b = _entity(db, EntityType.service, "SvcOnHostB")
            record_relationship(
                db, source=TopologySource.monitoring, relationship_type=RelationshipType.hosted_on,
                from_entity_id=svc_a, to_entity_id=host,
            )
            record_relationship(
                db, source=TopologySource.monitoring, relationship_type=RelationshipType.hosted_on,
                from_entity_id=svc_b, to_entity_id=host,
            )
            ev_a = _event(db, entity_id=svc_a, problem_type=t_a, last_seen=NOW)
            ev_b = _event(db, entity_id=svc_b, problem_type=t_b, last_seen=NOW + timedelta(seconds=10))
            db.commit()

            outcome = correlate_pair(db, ev_a.id, ev_b.id)
            assert outcome.correlated is False
            signals_seen = {h.signal.value for h in outcome.hits}
            assert "same_host" in signals_seen  # the signal IS real...
            assert "NOT_CORRELATED" in outcome.reason  # ...but insufficient alone
        finally:
            db.close()

    def test_temporal_only_events_do_not_correlate_no_matter_how_close(self, client):
        db = SessionLocal()
        try:
            t = _tag("ISOLATED")
            create_rule(
                db, rule_id=f"TEMP-PLUS-ENTITY-{t}", name="needs more than time",
                conditions=[{"event_type": t}, {"signals": ["same_entity"]}, {"time_window_seconds": 60}],
            )
            a_id = _entity(db, EntityType.service, "IsolatedServiceA")
            b_id = _entity(db, EntityType.service, "IsolatedServiceB")
            ev_a = _event(db, entity_id=a_id, problem_type=t, last_seen=NOW)
            ev_b = _event(db, entity_id=b_id, problem_type=t, last_seen=NOW + timedelta(seconds=1))
            db.commit()
            outcome = correlate_pair(db, ev_a.id, ev_b.id)
            assert outcome.correlated is False
        finally:
            db.close()


class TestTopologyLinkedEvents:
    def test_service_depends_on_db_correlates_via_known_dependency(self, client):
        db = SessionLocal()
        try:
            t_svc, t_db = _tag("TOPO_SVC"), _tag("TOPO_DB")
            create_rule(
                db, rule_id=f"DEP-RULE-{t_svc}", name="dependency",
                conditions=[{"event_type": t_svc}, {"signals": ["known_dependency"]},
                            {"time_window_seconds": 300}],
            )
            svc = _entity(db, EntityType.service, "OrdersSvc")
            database = _entity(db, EntityType.database, "OrdersDB")
            record_relationship(
                db, source=TopologySource.manual, relationship_type=RelationshipType.depends_on,
                from_entity_id=svc, to_entity_id=database, evidence="declared",
            )
            ev_svc = _event(db, entity_id=svc, problem_type=t_svc, last_seen=NOW)
            ev_db = _event(db, entity_id=database, problem_type=t_db, last_seen=NOW + timedelta(seconds=30))
            db.commit()

            outcome = correlate_pair(db, ev_svc.id, ev_db.id)
            assert outcome.correlated is True
            assert outcome.correlation_type.value == "dependency"  # manual source -> explicit, tier 2
            assert outcome.correlation_id is not None
        finally:
            db.close()


class TestHighAndLowConfidence:
    def test_multiple_strong_signals_score_higher_than_one(self, client):
        db = SessionLocal()
        try:
            t1, t2 = _tag("HICONF_A"), _tag("HICONF_B")
            create_rule(
                db, rule_id=f"MULTI-SIGNAL-{t1}", name="entity + trace",
                conditions=[{"event_type": t1}, {"signals": ["same_entity", "same_trace"]},
                            {"time_window_seconds": 300}],
            )
            t3, t4 = _tag("LOCONF_A"), _tag("LOCONF_B")
            create_rule(
                db, rule_id=f"ENTITY-ONLY-{t3}", name="entity only",
                conditions=[{"event_type": t3}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )

            high_entity = _entity(db, EntityType.service, "HighConfSvc")
            ev1 = _event(db, entity_id=high_entity, problem_type=t1, last_seen=NOW)
            ev2 = _event(db, entity_id=high_entity, problem_type=t2, last_seen=NOW + timedelta(seconds=5))
            _occurrence(db, logical_event=ev1, platform=SourcePlatform.zabbix, instance="I1",
                        external_id="hc-1", trace_id="trace-abc")
            _occurrence(db, logical_event=ev2, platform=SourcePlatform.dynatrace, instance="I2",
                        external_id="hc-2", trace_id="trace-abc")
            db.commit()
            high = correlate_pair(db, ev1.id, ev2.id)
            assert high.correlated is True

            low_entity = _entity(db, EntityType.service, "LowConfSvc")
            ev3 = _event(db, entity_id=low_entity, problem_type=t3, last_seen=NOW)
            ev4 = _event(db, entity_id=low_entity, problem_type=t4, last_seen=NOW + timedelta(seconds=5))
            db.commit()
            low = correlate_pair(db, ev3.id, ev4.id)
            assert low.correlated is True

            assert high.score > low.score
        finally:
            db.close()


class TestRulePriority:
    def test_exact_trace_rule_wins_over_entity_rule_when_both_match(self, client):
        db = SessionLocal()
        try:
            t1, t2 = _tag("PRIO_A"), _tag("PRIO_B")
            create_rule(
                db, rule_id=f"LOW-PRI-ENTITY-{t1}", name="entity (tier 4)",
                conditions=[{"event_type": t1}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )
            create_rule(
                db, rule_id=f"HIGH-PRI-TRACE-{t1}", name="trace (tier 1)",
                conditions=[{"event_type": t1}, {"signals": ["same_trace"]}, {"time_window_seconds": 300}],
            )
            entity = _entity(db, EntityType.service, "PriorityTestSvc")
            ev1 = _event(db, entity_id=entity, problem_type=t1, last_seen=NOW)
            ev2 = _event(db, entity_id=entity, problem_type=t2, last_seen=NOW + timedelta(seconds=2))
            _occurrence(db, logical_event=ev1, platform=SourcePlatform.zabbix, instance="I1",
                        external_id="pri-1", trace_id="trace-xyz")
            _occurrence(db, logical_event=ev2, platform=SourcePlatform.dynatrace, instance="I2",
                        external_id="pri-2", trace_id="trace-xyz")
            db.commit()

            outcome = correlate_pair(db, ev1.id, ev2.id)
            assert outcome.correlated is True
            assert outcome.matched_rule_id == f"HIGH-PRI-TRACE-{t1}"
            assert outcome.correlation_type.value == "trace"
        finally:
            db.close()

    def test_dependency_rule_wins_over_entity_rule(self, client):
        """Conflicting rules: for the SAME pair, one rule would correlate it
        as an ENTITY match, another as a DEPENDENCY match — priority
        (Explicit/Topology Dependency, tier 2/3, outranks Entity, tier 4)
        decides which one actually fires."""
        db = SessionLocal()
        try:
            t_svc, t_db = _tag("CONFLICT_SVC"), _tag("CONFLICT_DB")
            create_rule(
                db, rule_id=f"CONFLICT-ENTITY-{t_svc}", name="entity (tier 4)",
                conditions=[{"event_type": t_svc}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )
            create_rule(
                db, rule_id=f"CONFLICT-DEP-{t_svc}", name="dependency (tier 2/3)",
                conditions=[{"event_type": t_svc}, {"signals": ["known_dependency"]}, {"time_window_seconds": 300}],
            )
            svc = _entity(db, EntityType.service, "ConflictSvc")
            db_entity = _entity(db, EntityType.database, "ConflictDB")
            record_relationship(
                db, source=TopologySource.dynatrace, relationship_type=RelationshipType.depends_on,
                from_entity_id=svc, to_entity_id=db_entity,
            )
            ev_svc = _event(db, entity_id=svc, problem_type=t_svc, last_seen=NOW)
            ev_db = _event(db, entity_id=db_entity, problem_type=t_db, last_seen=NOW + timedelta(seconds=5))
            db.commit()

            outcome = correlate_pair(db, ev_svc.id, ev_db.id)
            assert outcome.correlated is True
            # known_dependency (tier 3, dynatrace-sourced) outranks same_entity (tier 4).
            assert outcome.matched_rule_id == f"CONFLICT-DEP-{t_svc}"
            assert outcome.correlation_type.value == "topology"
        finally:
            db.close()


class TestLifecycle:
    def test_a_third_event_joining_moves_the_group_to_updated(self, client):
        db = SessionLocal()
        try:
            t = _tag("LIFECYCLE_NET")
            create_rule(
                db, rule_id=f"LIFECYCLE-DEP-{t}", name="dependency",
                conditions=[{"event_type": t}, {"signals": ["known_dependency"]},
                            {"time_window_seconds": 600}],
            )
            switch = _entity(db, EntityType.network_device, "LifecycleSwitch1")
            host1 = _entity(db, EntityType.host, "LifecycleHost1")
            host2 = _entity(db, EntityType.host, "LifecycleHost2")
            record_relationship(db, source=TopologySource.monitoring,
                                 relationship_type=RelationshipType.connects_to,
                                 from_entity_id=switch, to_entity_id=host1)
            record_relationship(db, source=TopologySource.monitoring,
                                 relationship_type=RelationshipType.connects_to,
                                 from_entity_id=switch, to_entity_id=host2)
            ev_switch = _event(db, entity_id=switch, problem_type=t, last_seen=NOW)
            ev_host1 = _event(db, entity_id=host1, problem_type=_tag("LIFECYCLE_H1"), last_seen=NOW + timedelta(seconds=5))
            ev_host2 = _event(db, entity_id=host2, problem_type=_tag("LIFECYCLE_H2"), last_seen=NOW + timedelta(seconds=8))
            db.commit()

            out1 = correlate_pair(db, ev_switch.id, ev_host1.id)
            assert out1.status.value == "new"
            db.commit()

            out2 = correlate_pair(db, ev_switch.id, ev_host2.id)
            assert out2.correlation_id == out1.correlation_id  # same group, not a second one
            assert out2.status.value == "updated"
            db.commit()

            correlation = db.get(Correlation, out1.correlation_id)
            assert set(correlation.member_event_ids) == {ev_switch.id, ev_host1.id, ev_host2.id}
        finally:
            db.close()

    def test_resolving_every_member_marks_the_correlation_resolved(self, client):
        db = SessionLocal()
        try:
            t1, t2 = _tag("RESOLVE_A"), _tag("RESOLVE_B")
            create_rule(
                db, rule_id=f"RESOLVE-RULE-{t1}", name="entity",
                conditions=[{"event_type": t1}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
            )
            entity = _entity(db, EntityType.service, "ResolveTestSvc")
            ev1 = _event(db, entity_id=entity, problem_type=t1, last_seen=NOW,
                         status=LogicalEventStatus.resolved)
            ev2 = _event(db, entity_id=entity, problem_type=t2, last_seen=NOW + timedelta(seconds=5),
                         status=LogicalEventStatus.resolved)
            db.commit()
            outcome = correlate_pair(db, ev1.id, ev2.id)
            assert outcome.correlated is True
            assert outcome.status.value == "resolved"
        finally:
            db.close()


class TestAcceptanceCriteria:
    """The engine must distinguish a network switch failure + multiple
    downstream unreachable events from two unrelated application failures
    occurring simultaneously."""

    def test_network_failure_and_downstream_unreachable_events_correlate_as_network(self, client):
        db = SessionLocal()
        try:
            create_rule(
                db, rule_id="ACCEPT-NETWORK", name="Network Device Impact",
                conditions=[
                    {"event_type": "NETWORK_DEVICE_DOWN"},
                    {"relationship": "connects_to"},
                    {"time_window_seconds": 300},
                ],
            )
            switch = _entity(db, EntityType.network_device, "AcceptSwitch1")
            host_a = _entity(db, EntityType.host, "AcceptHostA")
            host_b = _entity(db, EntityType.host, "AcceptHostB")
            record_relationship(db, source=TopologySource.monitoring,
                                 relationship_type=RelationshipType.connects_to,
                                 from_entity_id=switch, to_entity_id=host_a)
            record_relationship(db, source=TopologySource.monitoring,
                                 relationship_type=RelationshipType.connects_to,
                                 from_entity_id=switch, to_entity_id=host_b)

            # Real network-device-down events use AVAILABILITY_DOWN, which
            # event_type_tag() turns into "NETWORK_DEVICE_DOWN" for a
            # network_device entity — used directly here to prove the rule
            # matches on the SAME tag a real switch-down event would carry,
            # not a synthetic one.
            ev_switch = _event(db, entity_id=switch, problem_type="AVAILABILITY_DOWN", last_seen=NOW)
            ev_a = _event(db, entity_id=host_a, problem_type=_tag("ACCEPT_HOST_A"), last_seen=NOW + timedelta(seconds=15))
            ev_b = _event(db, entity_id=host_b, problem_type=_tag("ACCEPT_HOST_B"), last_seen=NOW + timedelta(seconds=20))
            db.commit()

            out_a = correlate_pair(db, ev_switch.id, ev_a.id)
            assert out_a.correlated is True
            assert out_a.correlation_type.value == "network"
            db.commit()
            out_b = correlate_pair(db, ev_switch.id, ev_b.id)
            assert out_b.correlated is True
            assert out_b.correlation_id == out_a.correlation_id
            db.commit()

            correlation = db.get(Correlation, out_a.correlation_id)
            assert set(correlation.member_event_ids) == {ev_switch.id, ev_a.id, ev_b.id}

            # Now: two unrelated application failures, simultaneous, no
            # topology link, no shared entity/trace — must NOT correlate.
            app1 = _entity(db, EntityType.application, "UnrelatedApp1")
            app2 = _entity(db, EntityType.application, "UnrelatedApp2")
            app_tag = _tag("ACCEPT_APP_ERROR")
            ev_app1 = _event(db, entity_id=app1, problem_type=app_tag, last_seen=NOW)
            ev_app2 = _event(db, entity_id=app2, problem_type=app_tag, last_seen=NOW)
            db.commit()
            out_apps = correlate_pair(db, ev_app1.id, ev_app2.id)
            assert out_apps.correlated is False
            assert "NOT_CORRELATED" in out_apps.reason
        finally:
            db.close()


class TestAPI:
    def test_create_rule_list_weights_evaluate_and_list_correlations(self, client):
        t = _tag("API_TEST")
        r = client.post("/api/v1/correlation/rules", json={
            "rule_id": "API-RULE-1", "name": "API test rule",
            "conditions": [{"event_type": t}, {"signals": ["same_entity"]}, {"time_window_seconds": 300}],
        })
        assert r.status_code == 201
        assert r.json()["priority_tier"] == 4

        r2 = client.get("/api/v1/correlation/rules")
        assert r2.status_code == 200
        assert any(x["rule_id"] == "API-RULE-1" for x in r2.json())

        r3 = client.get("/api/v1/correlation/weights")
        assert r3.status_code == 200
        assert any(w["signal"] == "same_trace" and w["weight"] == 30.0 for w in r3.json())

        r4 = client.put("/api/v1/correlation/weights/same_entity", params={"weight": 99.0})
        assert r4.status_code == 200
        assert r4.json()["weight"] == 99.0

        db = SessionLocal()
        try:
            eid = _entity(db, EntityType.service, "APITestSvc")
            ev1 = _event(db, entity_id=eid, problem_type=t, last_seen=NOW)
            ev2 = _event(db, entity_id=eid, problem_type=_tag("API_TEST_B"), last_seen=NOW + timedelta(seconds=5))
            db.commit()
            ev1_id, ev2_id = ev1.id, ev2.id
        finally:
            db.close()

        r5 = client.post(
            "/api/v1/correlation/evaluate",
            params={"event_a_id": ev1_id, "event_b_id": ev2_id},
        )
        assert r5.status_code == 200
        body = r5.json()
        assert body["correlated"] is True
        # same_entity (updated to 99) + temporal_relationship (default 10),
        # both required by the matched rule — see evaluate_rule.
        assert body["score"] == 109.0
        cid = body["correlation_id"]

        r6 = client.get("/api/v1/correlations", params={"event_id": ev1_id})
        assert r6.status_code == 200
        assert any(c["id"] == cid for c in r6.json())

        r7 = client.get(f"/api/v1/correlations/{cid}")
        assert r7.status_code == 200
        detail = r7.json()
        assert detail["id"] == cid
        assert len(detail["evidence"]) >= 1

    def test_invalid_rule_is_rejected_with_422(self, client):
        r = client.post("/api/v1/correlation/rules", json={
            "rule_id": "API-BAD-RULE", "name": "bad",
            "conditions": [{"signals": ["same_host"]}, {"time_window_seconds": 300}],
        })
        assert r.status_code == 422

    def test_evaluate_unknown_event_is_404(self, client):
        r = client.post(
            "/api/v1/correlation/evaluate",
            params={"event_a_id": 999999, "event_b_id": 999998},
        )
        assert r.status_code == 404

    def test_unknown_correlation_is_404(self, client):
        assert client.get("/api/v1/correlations/999999").status_code == 404
