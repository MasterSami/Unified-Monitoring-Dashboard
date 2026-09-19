"""Correlation rule storage, validation, and weight configuration —
Correlation Phase 4.

Rules are configuration-driven (the task's own example is a flat list of
single-purpose condition objects — an ``event_type`` filter, a
``relationship`` requirement, a ``time_window_seconds`` bound); this module
merges that list into one spec, validates it, and derives the rule's
priority tier from it. See ``app.correlation_engine`` for how a merged rule
is actually evaluated against a pair of events.

False-correlation protection lives here, at rule-creation time: a rule
built only from ``temporal_relationship``, ``same_host``,
``same_application``, and/or ``multi_source`` — the four signals the task
explicitly says are never sufficient alone — is rejected outright rather
than silently accepted and later ignored, so a misconfigured rule fails
loudly at the point someone made the mistake.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CorrelationRule, CorrelationSignal, CorrelationWeight

#: Every signal, on its own or combined only with each other, that the task
#: explicitly forbids correlating on alone: "Never correlate solely because:
#: same timestamp [temporal_relationship], same host, same application."
#: multi_source joins them — two SEPARATE, unrelated problems can each be
#: independently confirmed by more than one tool; that alone says nothing
#: about whether THEY relate to each other.
WEAK_SIGNALS: frozenset[str] = frozenset({
    CorrelationSignal.temporal_relationship.value,
    CorrelationSignal.same_host.value,
    CorrelationSignal.same_application.value,
    CorrelationSignal.multi_source.value,
})

#: The six configurable temporal windows the task lists, in seconds.
ALLOWED_TIME_WINDOWS: frozenset[int] = frozenset({60, 120, 300, 600, 900, 1800})

#: Recommended priority order (1 = highest): Exact Trace, Explicit
#: Dependency, Topology, Entity, Temporal. known_dependency splits into two
#: tiers (2 or 3) depending on the connecting relationship's source — see
#: app.correlation_engine.SIGNAL_PRIORITY_TIER — a rule's OWN advertised
#: tier here uses the weaker (topology) tier, since which one actually
#: applies to a given match depends on the data, not the rule.
_SIGNAL_TIER = {
    CorrelationSignal.same_trace.value: 1,
    CorrelationSignal.known_dependency.value: 3,
    CorrelationSignal.same_entity.value: 4,
    CorrelationSignal.same_host.value: 4,
    CorrelationSignal.same_service.value: 4,
    CorrelationSignal.same_application.value: 4,
    CorrelationSignal.same_api.value: 4,
    CorrelationSignal.same_database.value: 4,
    CorrelationSignal.same_business_transaction.value: 4,
    CorrelationSignal.multi_source.value: 4,
    CorrelationSignal.temporal_relationship.value: 5,
}


def merge_conditions(conditions: list[dict]) -> dict:
    """Flatten the task's list-of-single-key-dicts condition style into one
    spec. Later entries win on key collision (last one written wins)."""
    merged: dict = {}
    for c in conditions:
        merged.update(c)
    return merged


def required_signals(merged: dict) -> set[str]:
    """Which signals a rule's merged conditions require, derived from the
    keys present — ``relationship``/``relationship_types`` implies
    known_dependency, any ``time_window_seconds`` implies
    temporal_relationship (it is the mandatory timing bound every rule has,
    not an optional extra), and an explicit ``signals`` list adds the rest.
    """
    out: set[str] = set(merged.get("signals", []))
    if "relationship" in merged or "relationship_types" in merged:
        out.add(CorrelationSignal.known_dependency.value)
    if merged.get("time_window_seconds") is not None:
        out.add(CorrelationSignal.temporal_relationship.value)
    return out


def validate_rule_conditions(conditions: list[dict]) -> dict:
    """Merge and validate a rule's conditions. Raises ValueError on any of:
    a missing/invalid time window, or a rule that (per WEAK_SIGNALS) could
    never be sufficient evidence to correlate on its own. Returns the merged
    spec for the caller to store/evaluate.
    """
    merged = merge_conditions(conditions)
    window = merged.get("time_window_seconds")
    if window is None:
        raise ValueError("a rule must specify time_window_seconds")
    if window not in ALLOWED_TIME_WINDOWS:
        raise ValueError(
            f"time_window_seconds must be one of {sorted(ALLOWED_TIME_WINDOWS)} "
            "(1/2/5/10/15/30 minutes)"
        )
    signals = required_signals(merged)
    strong = signals - WEAK_SIGNALS
    if not strong:
        raise ValueError(
            "a rule needs at least one signal beyond temporal_relationship/"
            "same_host/same_application/multi_source — those are never "
            "sufficient evidence on their own (false-correlation protection)"
        )
    return merged


def priority_tier_for_conditions(merged: dict) -> int:
    """The rule's advertised priority tier (1 highest .. 5 lowest) — the
    best (lowest-numbered) tier among its required signals.
    """
    signals = required_signals(merged)
    tiers = [_SIGNAL_TIER[s] for s in signals if s in _SIGNAL_TIER]
    return min(tiers) if tiers else 5


#: Seeded once, on a database that has no rules at all, so a fresh install
#: forms incidents from real data without someone first learning the rule
#: API. Each passes validate_rule_conditions on its own merits (one strong
#: signal + a bounded window); operators can edit or disable them like any
#: other rule and they are never re-seeded over a non-empty table.
DEFAULT_RULES: list[dict] = [
    {
        "rule_id": "SAMIX-SAME-TRACE",
        "name": "Same distributed trace",
        "conditions": [{"signals": ["same_trace"]}, {"time_window_seconds": 600}],
    },
    {
        "rule_id": "SAMIX-KNOWN-DEPENDENCY",
        "name": "Related through a known dependency (topology)",
        "conditions": [{"signals": ["known_dependency"]}, {"time_window_seconds": 1800}],
    },
    {
        "rule_id": "SAMIX-SAME-ENTITY",
        "name": "Multiple problems on the same entity",
        "conditions": [{"signals": ["same_entity"]}, {"time_window_seconds": 1800}],
    },
]


def ensure_default_rules(db: Session) -> int:
    """Create :data:`DEFAULT_RULES` if — and only if — no rule exists yet.
    Returns how many were created (0 on every later call)."""
    if db.scalar(select(CorrelationRule.id).limit(1)) is not None:
        return 0
    for spec in DEFAULT_RULES:
        create_rule(db, **spec)
    db.flush()
    return len(DEFAULT_RULES)


def create_rule(
    db: Session,
    *,
    rule_id: str,
    name: str,
    conditions: list[dict],
    enabled: bool = True,
    actions: dict | None = None,
) -> CorrelationRule:
    """Create or update a rule (upsert on ``rule_id``). Validates conditions
    first — see :func:`validate_rule_conditions`.
    """
    merged = validate_rule_conditions(conditions)
    tier = priority_tier_for_conditions(merged)
    existing = db.scalar(select(CorrelationRule).where(CorrelationRule.rule_id == rule_id))
    if existing is not None:
        existing.name = name
        existing.enabled = enabled
        existing.priority_tier = tier
        existing.conditions = conditions
        existing.actions = actions or {}
        db.flush()
        return existing
    row = CorrelationRule(
        rule_id=rule_id, name=name, enabled=enabled, priority_tier=tier,
        conditions=conditions, actions=actions or {},
    )
    db.add(row)
    db.flush()
    return row


def list_rules(db: Session, *, enabled_only: bool = False) -> list[CorrelationRule]:
    """Rules ordered by priority tier (highest priority first), then id for
    a stable, deterministic order among equal-tier rules.
    """
    stmt = select(CorrelationRule)
    if enabled_only:
        stmt = stmt.where(CorrelationRule.enabled.is_(True))
    stmt = stmt.order_by(CorrelationRule.priority_tier.asc(), CorrelationRule.id.asc())
    return list(db.scalars(stmt).all())


#: The task's own score example, plus reasoned defaults for the signals it
#: didn't list — weaker for the four WEAK_SIGNALS, comparable to
#: same_service/same_api for same_database (an equally concrete identity
#: match) and same_business_transaction (an explicit, if currently rare,
#: business-context match).
DEFAULT_WEIGHTS: dict[CorrelationSignal, float] = {
    CorrelationSignal.same_entity: 20.0,
    CorrelationSignal.same_host: 10.0,
    CorrelationSignal.same_service: 15.0,
    CorrelationSignal.same_application: 8.0,
    CorrelationSignal.same_api: 15.0,
    CorrelationSignal.same_database: 15.0,
    CorrelationSignal.same_trace: 30.0,
    CorrelationSignal.known_dependency: 25.0,
    CorrelationSignal.temporal_relationship: 10.0,
    CorrelationSignal.same_business_transaction: 15.0,
    CorrelationSignal.multi_source: 8.0,
}


def get_weights(db: Session) -> dict[CorrelationSignal, float]:
    """Every signal's current weight — a stored override if one exists,
    :data:`DEFAULT_WEIGHTS` otherwise. Always returns all 11 signals.
    """
    stored = {r.signal: r.weight for r in db.scalars(select(CorrelationWeight)).all()}
    return {sig: stored.get(sig, DEFAULT_WEIGHTS[sig]) for sig in CorrelationSignal}


def set_weight(db: Session, signal: CorrelationSignal, weight: float) -> CorrelationWeight:
    row = db.scalar(select(CorrelationWeight).where(CorrelationWeight.signal == signal))
    if row is not None:
        row.weight = weight
    else:
        row = CorrelationWeight(signal=signal, weight=weight)
        db.add(row)
    db.flush()
    return row
