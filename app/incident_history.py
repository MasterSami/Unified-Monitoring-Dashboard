"""Historical incident export — Correlation Phase 7 (AI-readiness,
architecture preparation only).

Assembles the FULL structured record of one incident — the task's own list
(section 1): the incident itself, its events, entities, topology, structured
correlation signals, which rules triggered, root cause candidates, operator
confirmation, resolution, resolution time, business impact, affected
services/APIs/databases, and the timeline — from data every prior phase
already writes. This module adds no new decision logic and no new stored
fact of its own (except reading Phase 7's IncidentResolution, added
alongside this file); it only assembles what is already there into one
export shape a future similarity/pattern-discovery/RCA-suggestion layer
could consume (see :mod:`app.ai_provider`'s own note on that boundary).

Nothing in :mod:`app.correlation_engine` or :mod:`app.incident_engine` calls
this — it is read-only, additive, and exists purely to be read by something
else later (a human via the API today; conceivably a future AI provider).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.incident_engine import _entities_of, _occurrences_of, build_timeline
from app.models import (
    CanonicalEntity,
    CorrelationEvidence,
    EntityRelationship,
    Incident,
    IncidentFeedback,
    IncidentResolution,
    LogicalEvent,
)


def _resolution_dict(row: IncidentResolution | None) -> dict | None:
    if row is None:
        return None
    return {
        "confirmed_root_cause_entity_id": row.confirmed_root_cause_entity_id,
        "resolution_action": row.resolution_action,
        "resolution_time": row.resolution_time,
        "resolver": row.resolver,
        "post_incident_notes": row.post_incident_notes,
    }


def export_incident_history(db: Session, incident_id: int) -> dict | None:
    """The complete structured history for one incident, or ``None`` if it
    does not exist. Every value here is read straight off existing tables —
    nothing is inferred, scored, or summarized.
    """
    incident = db.get(Incident, incident_id)
    if incident is None:
        return None

    member_ids = incident.related_events or []
    members = [m for m in (db.get(LogicalEvent, eid) for eid in member_ids) if m is not None]
    occurrences = _occurrences_of(db, members)
    entities = _entities_of(db, members)

    # Widen to the incident's own wider (topology-reachable) affected set
    # too, so "topology" here is the same complete picture the correlation
    # graph shows, not just the entities that themselves fired an event.
    affected_entity_ids: set[int] = set()
    for bucket in (incident.affected or {}).values():
        affected_entity_ids.update(item["entity_id"] for item in bucket)
    missing = affected_entity_ids - set(entities)
    if missing:
        for e in db.scalars(select(CanonicalEntity).where(CanonicalEntity.id.in_(missing))).all():
            entities[e.id] = e
    all_entity_ids = set(entities) | affected_entity_ids

    topology: list[dict] = []
    if all_entity_ids:
        rels = db.scalars(
            select(EntityRelationship).where(
                EntityRelationship.from_entity_id.in_(all_entity_ids),
                EntityRelationship.to_entity_id.in_(all_entity_ids),
            )
        ).all()
        topology = [
            {
                "id": r.id, "relationship_type": r.relationship_type.value,
                "from_entity_id": r.from_entity_id, "to_entity_id": r.to_entity_id,
                "source": r.source.value, "evidence": r.evidence, "confidence": r.confidence,
            }
            for r in rels
        ]

    evidence_rows = []
    if incident.source_correlation_ids:
        evidence_rows = list(
            db.scalars(
                select(CorrelationEvidence)
                .where(CorrelationEvidence.correlation_id.in_(incident.source_correlation_ids))
                .order_by(CorrelationEvidence.timestamp.asc())
            ).all()
        )
    correlation_signals = [
        {
            "signal": e.signal.value, "source": e.source, "value": e.value,
            "from_entity_id": e.from_entity_id, "to_entity_id": e.to_entity_id,
            "related_event_id": e.related_event_id, "rule_id": e.rule_id, "timestamp": e.timestamp,
        }
        for e in evidence_rows
    ]
    rules_triggered = sorted({e.rule_id for e in evidence_rows if e.rule_id})

    feedback_rows = db.scalars(
        select(IncidentFeedback)
        .where(IncidentFeedback.incident_id == incident_id)
        .order_by(IncidentFeedback.created_at.asc())
    ).all()
    operator_confirmation = [
        {
            "kind": f.kind.value, "note": f.note,
            "confirmed_root_cause_entity_id": f.confirmed_root_cause_entity_id,
            "actor": f.actor, "timestamp": f.created_at,
        }
        for f in feedback_rows
    ]

    resolution = _resolution_dict(
        db.scalar(select(IncidentResolution).where(IncidentResolution.incident_id == incident_id))
    )

    occurrences_by_event: dict[int, list] = defaultdict(list)
    for o in occurrences:
        occurrences_by_event[o.logical_event_id].append(o)
    events = [
        {
            "logical_event_id": m.id, "entity_id": m.entity_id,
            "normalized_problem_type": m.normalized_problem_type, "fingerprint": m.fingerprint,
            "status": m.status.value, "occurrence_count": m.occurrence_count,
            "first_seen": m.first_seen, "last_seen": m.last_seen,
            "occurrences": [
                {
                    "external_id": o.external_id, "source_platform": o.source_platform.value,
                    "source_instance": o.source_instance, "title": o.title,
                    "original_severity": o.original_severity, "severity_label": o.severity_label,
                    "started_at": o.started_at, "resolved": o.resolved, "resolved_at": o.resolved_at,
                    "trace_id": o.trace_id, "span_id": o.span_id, "business_service": o.business_service,
                }
                for o in occurrences_by_event.get(m.id, [])
            ],
        }
        for m in members
    ]

    entities_out = [
        {"entity_id": e.id, "entity_type": e.entity_type.value, "canonical_name": e.canonical_name}
        for e in entities.values()
    ]

    affected = incident.affected or {}
    return {
        "incident": {
            "id": incident.id, "title": incident.title, "status": incident.status.value,
            "severity_int": incident.severity_int, "severity_label": incident.severity_label,
            "start_time": incident.start_time, "last_update": incident.last_update,
            "business_service": incident.business_service, "correlation_types": incident.correlation_types,
            "sources": incident.sources, "source_correlation_ids": incident.source_correlation_ids,
        },
        "events": events,
        "entities": entities_out,
        "topology": topology,
        "correlation_signals": correlation_signals,
        "rules_triggered": rules_triggered,
        "root_cause_candidates": incident.root_cause_candidates or [],
        "operator_confirmation": operator_confirmation,
        "resolution": resolution,
        "resolution_time": resolution["resolution_time"] if resolution else None,
        "business_impact": {"business_service": incident.business_service, "affected": affected},
        "affected_services": affected.get("services", []),
        "affected_apis": affected.get("apis", []),
        "affected_databases": affected.get("databases", []),
        "timeline": build_timeline(db, members),
    }
