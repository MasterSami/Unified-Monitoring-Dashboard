from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from app.correlation_engine import correlate_pair
from app.correlation_rules import create_rule
from app.db import SessionLocal
from app.incident_engine import form_or_update_incident_from_correlation
from app.models import (
    Alert,
    CanonicalEntity,
    Correlation,
    EntityType,
    LogicalEvent,
    LogicalEventStatus,
    SourcePlatform,
)

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
_counter = itertools.count(1)


def _event(db, entity_id: int, problem_type: str, when: datetime, trace_id: str | None = None) -> LogicalEvent:
    event = LogicalEvent(
        fingerprint=f"v2:{next(_counter)}",
        entity_id=entity_id,
        normalized_problem_type=problem_type,
        status=LogicalEventStatus.open,
        occurrence_count=1,
        first_seen=when,
        last_seen=when,
        title=problem_type,
        current_severity_int=4,
        current_severity_label="High",
    )
    db.add(event)
    db.flush()
    db.add(Alert(
        source_platform=SourcePlatform.zabbix,
        source_instance="v2-test",
        external_id=f"alert-{next(_counter)}",
        severity_int=4,
        severity_label="High",
        title=problem_type,
        started_at=when,
        entity_id=entity_id,
        logical_event_id=event.id,
        trace_id=trace_id,
    ))
    db.flush()
    return event


def test_same_trace_is_high_confidence_and_persisted(client):
    db = SessionLocal()
    try:
        first = CanonicalEntity(entity_type=EntityType.service, canonical_name="V2 Service")
        db.add(first)
        db.flush()
        a = _event(db, first.id, "V2_A", NOW, "trace-v2")
        b = _event(db, first.id, "V2_B", NOW + timedelta(seconds=5), "trace-v2")
        create_rule(
            db,
            rule_id="V2-TRACE-TEST",
            name="V2 trace test",
            conditions=[{"signals": ["same_trace"]}, {"time_window_seconds": 300}],
        )
        db.commit()

        outcome = correlate_pair(db, a.id, b.id)
        assert outcome.correlated is True
        assert outcome.decision_version == "v2"
        assert outcome.decision_level == "high"
        assert outcome.decision_score >= 0.95
        assert any("trace" in line.lower() for line in outcome.positive_evidence)
        correlation = db.get(Correlation, outcome.correlation_id)
        assert correlation is not None
        assert correlation.decision_level == "high"
        assert correlation.decision_version == "v2"
        db.rollback()
    finally:
        db.close()


def test_incident_exposes_v2_confidence_and_missing_evidence(client):
    db = SessionLocal()
    try:
        entity = CanonicalEntity(entity_type=EntityType.service, canonical_name="V2 Review Service")
        db.add(entity)
        db.flush()
        a = _event(db, entity.id, "V2_REVIEW_A", NOW)
        b = _event(db, entity.id, "V2_REVIEW_B", NOW + timedelta(seconds=20))
        create_rule(
            db,
            rule_id="V2-ENTITY-TEST",
            name="V2 entity test",
            conditions=[{"signals": ["same_entity"]}, {"time_window_seconds": 300}],
        )
        db.commit()
        outcome = correlate_pair(db, a.id, b.id)
        correlation = db.get(Correlation, outcome.correlation_id)
        incident = form_or_update_incident_from_correlation(db, correlation)
        db.commit()
        assert incident is not None
        assert incident.decision_version == "v2"
        assert incident.confidence_level in {"medium", "high", "low"}
        assert incident.confidence_score > 0
        assert incident.missing_evidence
    finally:
        db.close()
