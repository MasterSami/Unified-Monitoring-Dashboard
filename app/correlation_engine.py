"""Deterministic correlation rule engine — Correlation Phase 4.

Decides whether two :class:`~app.models.LogicalEvent` rows (Phase 2's
deduplicated problems) describe the same real-world incident. No AI/ML:
every decision reduces to a fixed set of boolean signal checks
(:class:`~app.models.CorrelationSignal`), scored with configurable weights
(:mod:`app.correlation_rules`), and gated by configuration-driven rules.

The central guarantee (task section 8, "false correlation protection"): two
events are never correlated on temporal proximity, a shared host, or a
shared application alone — see :data:`app.correlation_rules.WEAK_SIGNALS`.
That is enforced twice: once when a rule is created (a rule built only from
weak signals is rejected — see ``app.correlation_rules.validate_rule_conditions``)
and again here, defensively, right before a match is accepted.

Everything is explainable: :func:`correlate_pair` returns exactly which
signals fired, their values, which rule (if any) matched, and — when
nothing matched — why not. Nothing is inferred that could not be read back
out of the returned :class:`CorrelationOutcome`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.correlation_rules import (
    WEAK_SIGNALS,
    get_weights,
    list_rules,
    merge_conditions,
)
from app.models import (
    Alert,
    CanonicalEntity,
    Correlation,
    CorrelationEvidence,
    CorrelationRule,
    CorrelationSignal,
    CorrelationStatus,
    CorrelationType,
    EntityRelationship,
    EntityType,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    TopologySource,
)

#: Priority tier per signal (1 highest .. 5 lowest) — see
#: app.correlation_rules._SIGNAL_TIER for the rule-level version of this.
#: known_dependency is resolved per-match here, not fixed: an
#: explicit/manual/cmdb-sourced relationship is tier 2 ("Explicit
#: Dependency"), a dynatrace/monitoring-sourced one is tier 3 ("Topology").
_EXPLICIT_TOPOLOGY_SOURCES = frozenset({
    TopologySource.explicit_config, TopologySource.cmdb, TopologySource.manual,
})

SIGNAL_PRIORITY_TIER: dict[CorrelationSignal, int] = {
    CorrelationSignal.same_trace: 1,
    CorrelationSignal.known_dependency: 3,  # overridden per-hit; see compute_signals
    CorrelationSignal.same_entity: 4,
    CorrelationSignal.same_host: 4,
    CorrelationSignal.same_service: 4,
    CorrelationSignal.same_application: 4,
    CorrelationSignal.same_api: 4,
    CorrelationSignal.same_database: 4,
    CorrelationSignal.same_business_transaction: 4,
    CorrelationSignal.multi_source: 4,
    CorrelationSignal.temporal_relationship: 5,
}

_TYPE_BY_SIGNAL: dict[CorrelationSignal, CorrelationType] = {
    CorrelationSignal.same_trace: CorrelationType.trace,
    CorrelationSignal.same_entity: CorrelationType.entity,
    CorrelationSignal.same_host: CorrelationType.entity,
    CorrelationSignal.same_service: CorrelationType.service,
    CorrelationSignal.same_application: CorrelationType.application,
    CorrelationSignal.same_api: CorrelationType.api,
    CorrelationSignal.same_database: CorrelationType.database,
    CorrelationSignal.same_business_transaction: CorrelationType.business_transaction,
    CorrelationSignal.multi_source: CorrelationType.multi_source,
    CorrelationSignal.temporal_relationship: CorrelationType.temporal,
}


@dataclass(frozen=True)
class SignalHit:
    """One TRUE signal between two events, ready to become evidence."""

    signal: CorrelationSignal
    value: str
    source: str
    priority_tier: int
    related_entity_id: int | None = None


@dataclass
class CorrelationOutcome:
    """The full, explainable result of evaluating one pair of events."""

    correlated: bool
    reason: str
    hits: list[SignalHit] = field(default_factory=list)
    score: float = 0.0
    correlation_type: CorrelationType | None = None
    matched_rule_id: str | None = None
    correlation_id: int | None = None
    status: CorrelationStatus | None = None


def _occurrences(db: Session, event: LogicalEvent) -> list[Alert]:
    return list(db.scalars(select(Alert).where(Alert.logical_event_id == event.id)).all())


def _entity(db: Session, entity_id: int | None) -> CanonicalEntity | None:
    return db.get(CanonicalEntity, entity_id) if entity_id is not None else None


def _host_of(db: Session, entity: CanonicalEntity | None) -> int | None:
    """The host entity_id underneath ``entity`` — itself if it already is a
    host, else the far end of a direct HOSTED_ON edge, else None.
    """
    if entity is None:
        return None
    if entity.entity_type == EntityType.host:
        return entity.id
    rel = db.scalars(
        select(EntityRelationship).where(
            EntityRelationship.from_entity_id == entity.id,
            EntityRelationship.relationship_type == RelationshipType.hosted_on,
        )
    ).first()
    return rel.to_entity_id if rel else None


def event_type_tag(db: Session, event: LogicalEvent) -> str:
    """A rule-matchable label for an event's kind, e.g. ``NETWORK_DEVICE_DOWN``
    for an AVAILABILITY_DOWN problem on a network_device entity, or just the
    normalized problem type (``CPU_HIGH``) otherwise.
    """
    entity = _entity(db, event.entity_id)
    if event.normalized_problem_type == "AVAILABILITY_DOWN" and entity is not None:
        return f"{entity.entity_type.value.upper()}_DOWN"
    return event.normalized_problem_type


def time_delta_seconds(a: LogicalEvent, b: LogicalEvent) -> float | None:
    """How far apart two events' most recent activity is, or None if either
    has no timestamp at all.
    """
    ta, tb = a.last_seen, b.last_seen
    if ta is None or tb is None:
        return None

    def aware(d: datetime) -> datetime:
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    return abs((aware(ta) - aware(tb)).total_seconds())


def compute_signals(db: Session, a: LogicalEvent, b: LogicalEvent) -> list[SignalHit]:
    """Every non-temporal signal that holds between ``a`` and ``b``.

    temporal_relationship is deliberately excluded here — whether it
    "counts" depends on a specific rule's own time_window_seconds (the task:
    "the time window must be rule-specific"), so it is resolved per-rule in
    :func:`evaluate_rule`, not once per pair.
    """
    hits: list[SignalHit] = []
    entity_a, entity_b = _entity(db, a.entity_id), _entity(db, b.entity_id)

    if a.entity_id is not None and a.entity_id == b.entity_id:
        hits.append(SignalHit(
            CorrelationSignal.same_entity, entity_a.canonical_name if entity_a else str(a.entity_id),
            "phase1", SIGNAL_PRIORITY_TIER[CorrelationSignal.same_entity], a.entity_id,
        ))
    else:
        host_a, host_b = _host_of(db, entity_a), _host_of(db, entity_b)
        if host_a is not None and host_a == host_b:
            host_entity = _entity(db, host_a)
            hits.append(SignalHit(
                CorrelationSignal.same_host, host_entity.canonical_name if host_entity else str(host_a),
                "phase3", SIGNAL_PRIORITY_TIER[CorrelationSignal.same_host], host_a,
            ))

    _TYPED_SIGNALS = (
        (CorrelationSignal.same_service, EntityType.service),
        (CorrelationSignal.same_application, EntityType.application),
        (CorrelationSignal.same_api, EntityType.api),
        (CorrelationSignal.same_database, EntityType.database),
    )
    if entity_a is not None and entity_b is not None and entity_a.id == entity_b.id:
        for signal, etype in _TYPED_SIGNALS:
            if entity_a.entity_type == etype:
                hits.append(SignalHit(
                    signal, entity_a.canonical_name, "phase1",
                    SIGNAL_PRIORITY_TIER[signal], entity_a.id,
                ))

    trace_a = {o.trace_id for o in _occurrences(db, a) if o.trace_id}
    trace_b = {o.trace_id for o in _occurrences(db, b) if o.trace_id}
    common_trace = trace_a & trace_b
    if common_trace:
        hits.append(SignalHit(
            CorrelationSignal.same_trace, sorted(common_trace)[0], "phase1",
            SIGNAL_PRIORITY_TIER[CorrelationSignal.same_trace],
        ))

    bt_a = {o.business_service for o in _occurrences(db, a) if o.business_service}
    bt_b = {o.business_service for o in _occurrences(db, b) if o.business_service}
    common_bt = bt_a & bt_b
    if common_bt:
        hits.append(SignalHit(
            CorrelationSignal.same_business_transaction, sorted(common_bt)[0], "phase1",
            SIGNAL_PRIORITY_TIER[CorrelationSignal.same_business_transaction],
        ))

    sources_a = {o.source_platform.value for o in _occurrences(db, a)}
    sources_b = {o.source_platform.value for o in _occurrences(db, b)}
    if len(sources_a) > 1 and len(sources_b) > 1:
        hits.append(SignalHit(
            CorrelationSignal.multi_source,
            f"{','.join(sorted(sources_a))} / {','.join(sorted(sources_b))}", "phase2",
            SIGNAL_PRIORITY_TIER[CorrelationSignal.multi_source],
        ))

    if a.entity_id is not None and b.entity_id is not None and a.entity_id != b.entity_id:
        rels = db.scalars(
            select(EntityRelationship).where(
                or_(
                    and_(EntityRelationship.from_entity_id == a.entity_id,
                         EntityRelationship.to_entity_id == b.entity_id),
                    and_(EntityRelationship.from_entity_id == b.entity_id,
                         EntityRelationship.to_entity_id == a.entity_id),
                )
            )
        ).all()
        for rel in rels:
            tier = 2 if rel.source in _EXPLICIT_TOPOLOGY_SOURCES else 3
            hits.append(SignalHit(
                CorrelationSignal.known_dependency,
                f"{rel.relationship_type.value} ({rel.source.value})",
                rel.source.value, tier, rel.id,
            ))

    return hits


def evaluate_rule(
    db: Session,
    rule: CorrelationRule,
    a: LogicalEvent,
    b: LogicalEvent,
    base_hits: list[SignalHit],
    delta_seconds: float | None,
) -> list[SignalHit] | None:
    """If ``rule`` matches this pair, the hits that satisfy it (base hits it
    required, plus a temporal hit if its window was met); else None.
    """
    merged = merge_conditions(rule.conditions)
    by_signal = {h.signal.value: h for h in base_hits}

    event_type = merged.get("event_type")
    if event_type is not None:
        tags = {event_type_tag(db, a), event_type_tag(db, b)}
        if event_type not in tags:
            return None

    window = merged.get("time_window_seconds")
    if window is None:
        return None  # validated at creation time, but never trust storage alone
    if delta_seconds is None or delta_seconds > window:
        return None
    matched: list[SignalHit] = [SignalHit(
        CorrelationSignal.temporal_relationship,
        f"{delta_seconds:.0f}s apart (within {window}s window)", "engine",
        SIGNAL_PRIORITY_TIER[CorrelationSignal.temporal_relationship],
    )]

    rel_types = merged.get("relationship_types")
    if merged.get("relationship") is not None:
        rel_types = [merged["relationship"]]
    if rel_types is not None:
        rel_types_lower = {str(r).lower() for r in rel_types}
        dep_hits = [
            h for h in base_hits
            if h.signal == CorrelationSignal.known_dependency
            and h.value.split(" ", 1)[0] in rel_types_lower
        ]
        if not dep_hits:
            return None
        matched.extend(dep_hits)
    elif CorrelationSignal.known_dependency.value in merged.get("signals", []):
        if CorrelationSignal.known_dependency.value not in by_signal:
            return None
        matched.append(by_signal[CorrelationSignal.known_dependency.value])

    for sig_name in merged.get("signals", []):
        if sig_name in (CorrelationSignal.known_dependency.value, CorrelationSignal.temporal_relationship.value):
            continue  # already handled above
        hit = by_signal.get(sig_name)
        if hit is None:
            return None
        matched.append(hit)

    return matched


def correlation_type_for(
    db: Session, hits: list[SignalHit], a: LogicalEvent, b: LogicalEvent,
) -> CorrelationType:
    """The best (lowest-tier) hit determines the type, with a NETWORK
    override when a network_device entity is on either side of a
    known_dependency match.
    """
    best = min(hits, key=lambda h: h.priority_tier)
    if best.signal == CorrelationSignal.known_dependency:
        entity_a, entity_b = _entity(db, a.entity_id), _entity(db, b.entity_id)
        if (entity_a and entity_a.entity_type == EntityType.network_device) or (
            entity_b and entity_b.entity_type == EntityType.network_device
        ):
            return CorrelationType.network
        return CorrelationType.dependency if best.priority_tier == 2 else CorrelationType.topology
    return _TYPE_BY_SIGNAL[best.signal]


def _score(weights: dict[CorrelationSignal, float], hits: list[SignalHit]) -> float:
    # A pair can produce more than one known_dependency hit (two sources
    # both recording the same edge) — score each contributing signal KIND
    # once, using its strongest (lowest-tier) hit, so re-confirmation by a
    # second source raises confidence in the evidence shown without
    # inflating the score by simple row count.
    best_per_signal: dict[CorrelationSignal, SignalHit] = {}
    for h in hits:
        cur = best_per_signal.get(h.signal)
        if cur is None or h.priority_tier < cur.priority_tier:
            best_per_signal[h.signal] = h
    return sum(weights[sig] for sig in best_per_signal)


def _find_existing_correlation(db: Session, event_id: int) -> Correlation | None:
    rows = db.scalars(
        select(Correlation).where(Correlation.status != CorrelationStatus.split)
    ).all()
    for c in rows:
        if event_id in (c.member_event_ids or []):
            return c
    return None


def _compute_correlation_status(
    members: list[LogicalEvent], *, was: CorrelationStatus, is_new: bool, membership_grew: bool,
) -> CorrelationStatus:
    """Pure function of the group's current membership — see the
    CorrelationStatus docstring in app/models.py for what each state means.
    Priority order: split (too few members) > resolved > reopened > new >
    updated > correlated (the steady state).
    """
    if len(members) < 2:
        return CorrelationStatus.split
    if all(m.status == LogicalEventStatus.resolved for m in members):
        return CorrelationStatus.resolved
    if was == CorrelationStatus.resolved:
        return CorrelationStatus.reopened
    if is_new:
        return CorrelationStatus.new
    if membership_grew:
        return CorrelationStatus.updated
    return CorrelationStatus.correlated


def _persist_correlation(
    db: Session,
    a: LogicalEvent,
    b: LogicalEvent,
    rule: CorrelationRule,
    hits: list[SignalHit],
    score: float,
    ctype: CorrelationType,
) -> Correlation:
    existing = _find_existing_correlation(db, a.id) or _find_existing_correlation(db, b.id)
    is_new = existing is None
    was_status = existing.status if existing else CorrelationStatus.new
    membership_grew = False

    if existing is None:
        correlation = Correlation(
            correlation_type=ctype, status=CorrelationStatus.new, rule_id=rule.rule_id,
            score=score, member_event_ids=[a.id, b.id],
        )
        db.add(correlation)
        db.flush()
    else:
        correlation = existing
        before = set(correlation.member_event_ids or [])
        after = before | {a.id, b.id}
        membership_grew = after != before
        correlation.member_event_ids = sorted(after)
        correlation.correlation_type = ctype
        correlation.rule_id = rule.rule_id
        correlation.score = max(correlation.score, score)

    members = [
        m for m in (db.get(LogicalEvent, eid) for eid in correlation.member_event_ids) if m is not None
    ]
    correlation.status = _compute_correlation_status(
        members, was=was_status, is_new=is_new, membership_grew=membership_grew,
    )
    db.flush()

    # Evidence is framed relative to `a` (the event this call anchors on) —
    # every hit says why `a` correlates with `b`.
    for h in hits:
        db.add(CorrelationEvidence(
            correlation_id=correlation.id, signal=h.signal, value=h.value, source=h.source,
            related_event_id=b.id, related_entity_id=h.related_entity_id,
        ))
    db.flush()
    return correlation


def correlate_pair(db: Session, event_a_id: int, event_b_id: int) -> CorrelationOutcome:
    """Evaluate whether two LogicalEvents should be correlated, and persist
    the result if so. Always explainable — see :class:`CorrelationOutcome`.
    """
    if event_a_id == event_b_id:
        return CorrelationOutcome(correlated=False, reason="an event cannot correlate with itself")

    a, b = db.get(LogicalEvent, event_a_id), db.get(LogicalEvent, event_b_id)
    if a is None or b is None:
        return CorrelationOutcome(correlated=False, reason="one or both events not found")

    base_hits = compute_signals(db, a, b)
    delta = time_delta_seconds(a, b)
    weights = get_weights(db)

    best_rule: CorrelationRule | None = None
    best_hits: list[SignalHit] = []
    for rule in list_rules(db, enabled_only=True):
        matched = evaluate_rule(db, rule, a, b, base_hits, delta)
        if matched is None:
            continue
        strong = {h.signal.value for h in matched} - WEAK_SIGNALS
        if not strong:
            continue  # defense in depth — should already be unreachable via rule validation
        best_rule = rule
        best_hits = matched
        break  # rules are priority-ordered; first match wins

    if best_rule is None:
        weak_only = [h for h in base_hits if h.signal.value in WEAK_SIGNALS]
        reason = "NOT_CORRELATED: no rule matched"
        if weak_only and not any(h.signal.value not in WEAK_SIGNALS for h in base_hits):
            reason = (
                "NOT_CORRELATED: only weak signals present ("
                + ", ".join(sorted({h.signal.value for h in weak_only}))
                + ") — insufficient evidence per false-correlation protection"
            )
        elif not base_hits:
            reason = "NOT_CORRELATED: no signals matched between these events"
        return CorrelationOutcome(correlated=False, reason=reason, hits=base_hits)

    score = _score(weights, best_hits)
    ctype = correlation_type_for(db, best_hits, a, b)
    correlation = _persist_correlation(db, a, b, best_rule, best_hits, score, ctype)

    return CorrelationOutcome(
        correlated=True,
        reason=f"CORRELATED via rule {best_rule.rule_id} ({ctype.value})",
        hits=best_hits, score=score, correlation_type=ctype,
        matched_rule_id=best_rule.rule_id, correlation_id=correlation.id,
        status=correlation.status,
    )


def find_candidate_events(db: Session, event: LogicalEvent, *, window_seconds: int = 1800) -> list[LogicalEvent]:
    """A bounded set of OTHER active LogicalEvents worth evaluating ``event``
    against — never a full-table scan. Candidates are: events on the same
    entity, events one hop away in the dependency graph, and events active
    within ``window_seconds`` (the widest allowed rule window by default).
    """
    if event.entity_id is None:
        return []
    candidate_ids: set[int] = set()

    same_entity = db.scalars(
        select(LogicalEvent.id).where(
            LogicalEvent.entity_id == event.entity_id, LogicalEvent.id != event.id,
        )
    ).all()
    candidate_ids.update(same_entity)

    neighbor_ids = set()
    for rel in db.scalars(
        select(EntityRelationship).where(
            or_(
                EntityRelationship.from_entity_id == event.entity_id,
                EntityRelationship.to_entity_id == event.entity_id,
            )
        )
    ).all():
        neighbor_ids.add(rel.from_entity_id)
        neighbor_ids.add(rel.to_entity_id)
    neighbor_ids.discard(event.entity_id)
    if neighbor_ids:
        neighbor_events = db.scalars(
            select(LogicalEvent.id).where(LogicalEvent.entity_id.in_(neighbor_ids))
        ).all()
        candidate_ids.update(neighbor_events)

    if event.last_seen is not None:
        lo = event.last_seen.replace(tzinfo=event.last_seen.tzinfo or timezone.utc)
        from datetime import timedelta

        window_events = db.scalars(
            select(LogicalEvent.id).where(
                LogicalEvent.id != event.id,
                LogicalEvent.last_seen.is_not(None),
                LogicalEvent.last_seen >= lo - timedelta(seconds=window_seconds),
                LogicalEvent.last_seen <= lo + timedelta(seconds=window_seconds),
            )
        ).all()
        candidate_ids.update(window_events)

    if not candidate_ids:
        return []
    return list(db.scalars(select(LogicalEvent).where(LogicalEvent.id.in_(candidate_ids))).all())


def run_correlation_for_event(db: Session, event_id: int) -> list[CorrelationOutcome]:
    """Evaluate one LogicalEvent against every candidate worth checking.
    Returns every outcome (correlated or not), most useful for tests/API
    explainability; callers that only care about persisted correlations can
    filter on ``.correlated``.
    """
    event = db.get(LogicalEvent, event_id)
    if event is None:
        return []
    outcomes = []
    for candidate in find_candidate_events(db, event):
        outcomes.append(correlate_pair(db, event.id, candidate.id))
    return outcomes


def run_correlation_batch(db: Session, event_ids: list[int] | None = None, *, limit: int = 200) -> dict:
    """Run correlation for a batch of events (default: the most recently
    touched non-resolved LogicalEvents, capped at ``limit``). Returns a
    summary, not the full per-pair detail — use run_correlation_for_event
    for that.
    """
    if event_ids is None:
        event_ids = list(
            db.scalars(
                select(LogicalEvent.id)
                .where(LogicalEvent.status != LogicalEventStatus.resolved)
                .order_by(LogicalEvent.last_seen.desc().nullslast())
                .limit(limit)
            ).all()
        )
    evaluated = 0
    correlated = 0
    for eid in event_ids:
        for outcome in run_correlation_for_event(db, eid):
            evaluated += 1
            if outcome.correlated:
                correlated += 1
    return {"events_processed": len(event_ids), "pairs_evaluated": evaluated, "pairs_correlated": correlated}
