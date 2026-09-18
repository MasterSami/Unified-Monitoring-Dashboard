"""Incident formation — Correlation Phase 5.

Turns one or more Phase 4 :class:`~app.models.Correlation` groups into a
first-class :class:`~app.models.Incident`: a title, a severity, a status, who
it affects, a chronological timeline, and — where the evidence actually
supports it — which entity most likely caused it. No AI/ML: root-cause
ranking is a deterministic graph question (which member entity is nothing
else in THIS incident downstream of?) plus stored evidence, never a learned
score; every field is recomputed from ``related_events``/
``source_correlation_ids`` on every touch, never hand-maintained, so it can
never drift from what those groups currently show — same "pure function of
current data" discipline as LogicalEventStatus (Phase 2) and
CorrelationStatus (Phase 4).

Root cause is always "candidates", plural, each with its own evidence list —
never a single claimed-certain cause (task section 9/14: "do not fabricate
certainty"). When the group's own dependency graph doesn't distinguish
between two equally-plausible origins, both come back as candidates.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependency_graph import DEFAULT_MAX_DEPTH, traverse
from app.models import (
    Alert,
    CanonicalEntity,
    Correlation,
    CorrelationEvidence,
    CorrelationStatus,
    EntityType,
    Incident,
    IncidentStatus,
    LogicalEvent,
    LogicalEventStatus,
    SymptomRole,
)

#: Same "still an active problem" set app.application_health/
#: app.correlation_engine use.
ACTIVE_STATUSES = frozenset({
    LogicalEventStatus.open, LogicalEventStatus.updated,
    LogicalEventStatus.deduplicated, LogicalEventStatus.reopened,
})

_AFFECTED_BUCKETS: dict[EntityType, str] = {
    EntityType.application: "applications",
    EntityType.service: "services",
    EntityType.api: "apis",
    EntityType.database: "databases",
    EntityType.host: "hosts",
    EntityType.network_device: "network_devices",
}


def _aware(d: datetime) -> datetime:
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _entities_of(db: Session, events: list[LogicalEvent]) -> dict[int, CanonicalEntity]:
    ids = {e.entity_id for e in events if e.entity_id is not None}
    if not ids:
        return {}
    return {e.id: e for e in db.scalars(select(CanonicalEntity).where(CanonicalEntity.id.in_(ids))).all()}


def _occurrences_of(db: Session, member_events: list[LogicalEvent]) -> list[Alert]:
    event_ids = [e.id for e in member_events]
    if not event_ids:
        return []
    return list(db.scalars(select(Alert).where(Alert.logical_event_id.in_(event_ids))).all())


def root_cause_candidates(
    db: Session, member_events: list[LogicalEvent], *, correlation_ids: list[int] | None = None,
) -> list[dict]:
    """Every member entity that nothing ELSE in this same incident is
    upstream of — i.e. within this group's own dependency graph, nothing
    here explains why IT failed. Ranked earliest-onset first; each entry
    carries its own evidence, so a genuine tie (no dependency edge between
    two candidates) surfaces as two candidates, not a coin flip.
    """
    entities = _entities_of(db, member_events)
    entity_ids = set(entities)
    if not entity_ids:
        return []

    events_by_entity: dict[int, list[LogicalEvent]] = defaultdict(list)
    for ev in member_events:
        if ev.entity_id is not None:
            events_by_entity[ev.entity_id].append(ev)

    # X is downstream of (caused by) Y within this group if Y's own
    # downstream traversal reaches X. A candidate is any entity nothing else
    # in the group is downstream-reachable to.
    caused_by: dict[int, set[int]] = defaultdict(set)
    downstream_within_group: dict[int, set[int]] = {}
    for y in entity_ids:
        result = traverse(db, y, direction="downstream", max_depth=DEFAULT_MAX_DEPTH)
        reached = {n.entity_id for n in result.nodes} & entity_ids
        reached.discard(y)
        downstream_within_group[y] = reached
        for x in reached:
            caused_by[x].add(y)

    candidate_ids = [e for e in entity_ids if not caused_by.get(e)]

    def earliest(e: int) -> datetime:
        times = [_aware(ev.first_seen) for ev in events_by_entity[e] if ev.first_seen]
        return min(times) if times else datetime.max.replace(tzinfo=timezone.utc)

    all_times = [_aware(ev.first_seen) for ev in member_events if ev.first_seen]
    overall_earliest = min(all_times) if all_times else None
    candidate_ids.sort(key=earliest)

    evidence_by_entity: dict[int, list[CorrelationEvidence]] = defaultdict(list)
    if correlation_ids:
        for row in db.scalars(
            select(CorrelationEvidence).where(CorrelationEvidence.correlation_id.in_(correlation_ids))
        ).all():
            if row.related_entity_id is not None:
                evidence_by_entity[row.related_entity_id].append(row)

    call_mentions: dict[str, int] = defaultdict(int)
    for o in _occurrences_of(db, member_events):
        for name in (o.db_calls or []) + (o.external_calls or []):
            call_mentions[name] += 1

    out: list[dict] = []
    for e in candidate_ids:
        entity = entities[e]
        evs = events_by_entity[e]
        t0 = earliest(e)
        evidence: list[str] = []
        if overall_earliest is not None and t0 == overall_earliest:
            evidence.append(f"{entity.canonical_name}: earliest problem onset in this incident ({t0.isoformat()})")
        for x in sorted(downstream_within_group.get(e, set())):
            evidence.append(f"{entities[x].canonical_name} depends on {entity.canonical_name}")
        for row in evidence_by_entity.get(e, []):
            evidence.append(f"{row.signal.value}: {row.value} (source: {row.source})")
        if len(evs) > 1:
            evidence.append(f"{len(evs)} correlated occurrences point at {entity.canonical_name}")
        hits = call_mentions.get(entity.canonical_name, 0)
        if hits:
            evidence.append(f"{hits} traced request(s) called {entity.canonical_name}")
        out.append({
            "entity_id": e,
            "entity_type": entity.entity_type.value,
            "canonical_name": entity.canonical_name,
            "evidence": evidence,
            "evidence_count": len(evidence),
        })
    return out


def build_timeline(db: Session, member_events: list[LogicalEvent]) -> list[dict]:
    """Every occurrence's onset, and — when it has since recovered — its
    recovery, in chronological order. Recovery entries are what makes
    "DB recovered -> Service recovered -> API recovered" show up as one
    ordered sequence (task section 13): they are just later rows in the same
    timeline, already tied to this incident by membership, nothing separate
    to correlate.
    """
    entities = _entities_of(db, member_events)
    entity_by_event = {e.id: e.entity_id for e in member_events}
    entries: list[dict] = []
    for o in _occurrences_of(db, member_events):
        entity = entities.get(entity_by_event.get(o.logical_event_id))
        name = entity.canonical_name if entity else (o.host_hostname or o.title or "unknown")
        if o.started_at:
            entries.append({
                "timestamp": _aware(o.started_at), "event_id": o.logical_event_id,
                "entity_id": entity.id if entity else None, "entity_name": name,
                "description": f"{name}: {o.title}", "kind": "onset",
                "source_platform": o.source_platform.value,
            })
        if o.resolved and o.resolved_at:
            entries.append({
                "timestamp": _aware(o.resolved_at), "event_id": o.logical_event_id,
                "entity_id": entity.id if entity else None, "entity_name": name,
                "description": f"{name}: recovered", "kind": "recovery",
                "source_platform": o.source_platform.value,
            })
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def classify_members(member_events: list[LogicalEvent], root_cause_entity_ids: set[int]) -> dict[str, str]:
    """PRIMARY for a member on a root_cause_candidate entity, RELATED_SYMPTOM
    otherwise. (INDEPENDENT is not assigned here — it describes an event that
    was evaluated and NOT included; see SymptomRole's own docstring.)
    """
    roles: dict[str, str] = {}
    for ev in member_events:
        role = SymptomRole.primary if ev.entity_id in root_cause_entity_ids else SymptomRole.related_symptom
        roles[str(ev.id)] = role.value
    return roles


def _severity(member_events: list[LogicalEvent]) -> tuple[int, str]:
    active = [e for e in member_events if e.status != LogicalEventStatus.resolved] or member_events
    rep = max(active, key=lambda e: e.current_severity_int)
    return rep.current_severity_int, rep.current_severity_label


def _shared_business_service(occurrences: list[Alert]) -> str | None:
    values = {o.business_service for o in occurrences if o.business_service}
    return next(iter(values)) if len(values) == 1 else None


def _affected_entities(
    db: Session, member_entities: dict[int, CanonicalEntity], candidates: list[dict],
) -> dict[int, CanonicalEntity]:
    """The full blast radius: every member entity, plus everything reachable
    DOWNSTREAM from each root-cause candidate (task sections 3/6/7 expect
    "Affected Service: CustomerService" even when CustomerService never fired
    its own alert — its involvement is the recorded provides/depends_on
    topology, not a guess). Falls back to just the member entities when there
    are no candidates (e.g. entities with no topology declared at all).
    """
    ids = set(member_entities)
    for c in candidates:
        result = traverse(db, c["entity_id"], direction="downstream", max_depth=DEFAULT_MAX_DEPTH)
        ids.update(n.entity_id for n in result.nodes)
    missing = ids - set(member_entities)
    if not missing:
        return member_entities
    entities = dict(member_entities)
    for e in db.scalars(select(CanonicalEntity).where(CanonicalEntity.id.in_(missing))).all():
        entities[e.id] = e
    return entities


def _compute_affected(entities: dict[int, CanonicalEntity]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {name: [] for name in _AFFECTED_BUCKETS.values()}
    for entity in entities.values():
        key = _AFFECTED_BUCKETS.get(entity.entity_type)
        if key is not None:
            buckets[key].append({"entity_id": entity.id, "canonical_name": entity.canonical_name})
    for bucket in buckets.values():
        bucket.sort(key=lambda d: d["entity_id"])
    return buckets


def _build_title(candidates: list[dict], business_service: str | None, member_events: list[LogicalEvent]) -> str:
    if candidates:
        cause = candidates[0]["canonical_name"]
    elif member_events:
        cause = member_events[0].title or member_events[0].normalized_problem_type or "Unknown"
    else:
        cause = "Unknown"
    if business_service:
        return f"{business_service}: {cause} incident"
    return f"{cause} incident"


def _compute_incident_status(
    members: list[LogicalEvent], *, was: IncidentStatus, is_new: bool, membership_grew: bool,
) -> IncidentStatus:
    """Pure function of current membership — priority order: merged (sticky,
    terminal) > resolved > reopened > new(open) > updated > open (steady
    state). Mirrors app.correlation_engine._compute_correlation_status.
    """
    if was == IncidentStatus.merged:
        return IncidentStatus.merged
    if all(m.status == LogicalEventStatus.resolved for m in members):
        return IncidentStatus.resolved
    if was == IncidentStatus.resolved:
        return IncidentStatus.reopened
    if is_new:
        return IncidentStatus.open
    if membership_grew:
        return IncidentStatus.updated
    return IncidentStatus.open


def recompute_incident(
    db: Session, incident: Incident, *, is_new: bool = False, membership_grew: bool = False,
) -> None:
    """Recompute every summary field from ``incident.related_events`` /
    ``incident.source_correlation_ids`` — never hand-incremented. A caller
    that changed membership must set those two fields FIRST, then call this.
    """
    member_ids = incident.related_events or []
    members = [m for m in (db.get(LogicalEvent, eid) for eid in member_ids) if m is not None]
    if not members:
        return

    entities = _entities_of(db, members)
    occurrences = _occurrences_of(db, members)

    incident.status = _compute_incident_status(
        members, was=incident.status, is_new=is_new, membership_grew=membership_grew,
    )
    incident.severity_int, incident.severity_label = _severity(members)
    starts = [_aware(m.first_seen) for m in members if m.first_seen]
    if starts:
        incident.start_time = min(starts)
    incident.business_service = _shared_business_service(occurrences)

    candidates = root_cause_candidates(db, members, correlation_ids=incident.source_correlation_ids)
    incident.root_cause_candidates = candidates
    root_entity_ids = {c["entity_id"] for c in candidates}
    incident.member_roles = classify_members(members, root_entity_ids)
    incident.affected = _compute_affected(_affected_entities(db, entities, candidates))

    if incident.source_correlation_ids:
        types = db.scalars(
            select(Correlation.correlation_type).where(Correlation.id.in_(incident.source_correlation_ids))
        ).all()
        incident.correlation_types = sorted({t.value for t in types})
    else:
        incident.correlation_types = []

    incident.title = _build_title(candidates, incident.business_service, members)


def _find_incident_for_correlation(db: Session, correlation_id: int) -> Incident | None:
    """Linear scan over non-merged incidents — same accepted simplification
    as app.correlation_engine._find_existing_correlation for this scale.
    """
    for inc in db.scalars(select(Incident).where(Incident.status != IncidentStatus.merged)).all():
        if correlation_id in (inc.source_correlation_ids or []):
            return inc
    return None


def form_or_update_incident_from_correlation(db: Session, correlation: Correlation) -> Incident | None:
    """One Correlation group -> one Incident, created the first time a
    correlation qualifies and kept in sync afterward. None if the
    correlation has fewer than 2 active members — nothing to incident-ize
    (mirrors Correlation's own ``split`` state).
    """
    if correlation.status == CorrelationStatus.split:
        return None
    member_ids = correlation.member_event_ids or []
    if len(member_ids) < 2:
        return None

    existing = _find_incident_for_correlation(db, correlation.id)
    is_new = existing is None
    membership_grew = False
    if existing is None:
        incident = Incident(source_correlation_ids=[correlation.id], related_events=sorted(member_ids))
        db.add(incident)
        db.flush()
    else:
        incident = existing
        before = set(incident.related_events or [])
        after = before | set(member_ids)
        membership_grew = after != before
        incident.related_events = sorted(after)
        if correlation.id not in (incident.source_correlation_ids or []):
            incident.source_correlation_ids = sorted(set(incident.source_correlation_ids or []) | {correlation.id})

    recompute_incident(db, incident, is_new=is_new, membership_grew=membership_grew)
    db.flush()
    return incident


def run_incident_formation(db: Session, correlation_ids: list[int] | None = None, *, limit: int = 200) -> dict:
    """Form/update incidents from existing Phase 4 correlations. Default
    scope: every non-split correlation, most recent first, capped at
    ``limit`` — same shape as app.correlation_engine.run_correlation_batch.
    """
    if correlation_ids is None:
        correlation_ids = list(
            db.scalars(
                select(Correlation.id)
                .where(Correlation.status != CorrelationStatus.split)
                .order_by(Correlation.id.desc())
                .limit(limit)
            ).all()
        )
    formed = 0
    updated = 0
    for cid in correlation_ids:
        correlation = db.get(Correlation, cid)
        if correlation is None:
            continue
        existed_before = _find_incident_for_correlation(db, cid) is not None
        incident = form_or_update_incident_from_correlation(db, correlation)
        if incident is None:
            continue
        if existed_before:
            updated += 1
        else:
            formed += 1
    return {
        "correlations_processed": len(correlation_ids),
        "incidents_formed": formed,
        "incidents_updated": updated,
    }


def merge_incidents(db: Session, incident_ids: list[int]) -> Incident:
    """Merge 2+ incidents into the lowest-id survivor. Only ever called
    explicitly (task section 12: "only merge when deterministic evidence
    supports it") — the engine never merges on its own initiative; a caller
    naming specific incident ids IS the deterministic evidence. The other
    incidents are marked ``merged`` and kept, not deleted.
    """
    incidents = [db.get(Incident, iid) for iid in incident_ids]
    incidents = [i for i in incidents if i is not None and i.status != IncidentStatus.merged]
    if len(incidents) < 2:
        raise ValueError("merge needs at least two distinct, not-already-merged incidents")

    survivor = min(incidents, key=lambda i: i.id)
    others = [i for i in incidents if i.id != survivor.id]

    all_events = set(survivor.related_events or [])
    all_correlations = set(survivor.source_correlation_ids or [])
    for other in others:
        all_events |= set(other.related_events or [])
        all_correlations |= set(other.source_correlation_ids or [])
        other.status = IncidentStatus.merged
        other.merged_into_id = survivor.id

    survivor.related_events = sorted(all_events)
    survivor.source_correlation_ids = sorted(all_correlations)
    recompute_incident(db, survivor, membership_grew=True)
    db.flush()
    return survivor


def _correlation_overlaps(db: Session, correlation_id: int, event_ids: set[int]) -> bool:
    correlation = db.get(Correlation, correlation_id)
    return correlation is not None and bool(set(correlation.member_event_ids or []) & event_ids)


def split_incident(db: Session, incident_id: int, event_ids: list[int]) -> Incident:
    """Carve ``event_ids`` (a subset of ``incident_id``'s current members)
    out into a brand-new Incident. The original keeps everything else —
    never deletes a member, per "never delete symptoms".
    """
    original = db.get(Incident, incident_id)
    if original is None:
        raise ValueError(f"incident {incident_id} not found")
    current = set(original.related_events or [])
    moving = set(event_ids) & current
    if not moving:
        raise ValueError("none of the given events belong to this incident")
    remaining = current - moving
    if not remaining:
        raise ValueError("cannot split out every member — the original incident must keep at least one")

    original_correlation_ids = list(original.source_correlation_ids or [])

    original.related_events = sorted(remaining)
    original.source_correlation_ids = [
        cid for cid in original_correlation_ids if _correlation_overlaps(db, cid, remaining)
    ]
    recompute_incident(db, original)

    # A correlation that still covers the moving-out events, preferring the
    # original's own list; falling back to any correlation touching them at
    # all only if the original's own list happened to cover none (e.g. every
    # source correlation was already entirely on the `remaining` side).
    new_correlation_ids = [cid for cid in original_correlation_ids if _correlation_overlaps(db, cid, moving)]
    if not new_correlation_ids:
        new_correlation_ids = _all_correlation_ids_touching(db, moving)

    new_incident = Incident(related_events=sorted(moving), source_correlation_ids=new_correlation_ids)
    db.add(new_incident)
    db.flush()
    recompute_incident(db, new_incident, is_new=True)
    db.flush()
    return new_incident


def _all_correlation_ids_touching(db: Session, event_ids: set[int]) -> list[int]:
    """Every Correlation (any status) with at least one member in
    ``event_ids`` — the split's fallback when the ORIGINAL incident's own
    source_correlation_ids list (post-split) no longer references a
    correlation that still legitimately covers the events moving out.
    """
    out = []
    for c in db.scalars(select(Correlation)).all():
        if set(c.member_event_ids or []) & event_ids:
            out.append(c.id)
    return out
