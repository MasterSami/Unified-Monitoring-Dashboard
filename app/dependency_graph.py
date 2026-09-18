"""Deterministic topology / dependency graph — Correlation Phase 3.

A graph over :class:`~app.models.CanonicalEntity` nodes, edged by
:class:`~app.models.EntityRelationship` rows. No AI/ML: every traversal is a
plain breadth-first search over rows already in the database, with fixed,
documented rules for direction and depth. See ``app.topology_sync`` for how
relationships get populated from Dynatrace/NNMi, and the API layer
(``app.routers.api``) for how a human or another phase queries it.

Traversal direction
--------------------
Every :class:`~app.models.RelationshipType` falls into one of two families
(see its own docstring for the reasoning):

- ``DEPENDENCY_TYPES`` (depends_on, calls, hosted_on, connects_to,
  routes_to, uses): stored as *from* needs *to*. A failure of ``to``
  propagates backward to ``from``.
- ``IMPACT_TYPES`` (affects, provides, part_of, member_of): stored already
  in the failure-propagation direction — *from*'s failure propagates
  forward to ``to``.

"Upstream" (what does X depend on) and "downstream" (what's affected if X
fails) are mirror images of each other under this rule — see
:func:`_neighbors` for the single place that encodes it.

Cycle protection
-----------------
Every traversal carries a ``visited`` set (an entity is expanded at most
once, however many paths reach it) and a ``max_depth`` bound (default 5,
capped at :data:`MAX_ALLOWED_DEPTH` regardless of what a caller asks for).
A graph containing a cycle (A depends on B depends on A) terminates exactly
like an acyclic one — the second time A is reached it is already in
``visited`` and is not expanded again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CanonicalEntity, EntityRelationship, RelationshipType, TopologySource

#: from_entity needs to_entity; a failure of to_entity propagates BACKWARD
#: (to -> from).
DEPENDENCY_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.depends_on, RelationshipType.calls, RelationshipType.hosted_on,
    RelationshipType.connects_to, RelationshipType.routes_to, RelationshipType.uses,
})

#: from_entity's failure propagates FORWARD (from -> to), already stored in
#: the direction impact travels. part_of/member_of belong here, not above —
#: see the RelationshipType docstring: a part's trouble propagates to the
#: whole it's part of, the same direction the edge is stored in.
IMPACT_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.affects, RelationshipType.provides,
    RelationshipType.part_of, RelationshipType.member_of,
})

#: A caller-requested max_depth is clamped to this regardless — an unbounded
#: traversal request must never be able to walk the whole table.
MAX_ALLOWED_DEPTH = 25
DEFAULT_MAX_DEPTH = 5


def record_relationship(
    db: Session,
    *,
    source: TopologySource,
    relationship_type: RelationshipType,
    from_entity_id: int,
    to_entity_id: int,
    source_reference: str = "",
    evidence: str | None = None,
    confidence: float | None = None,
) -> EntityRelationship:
    """Idempotent upsert on (source, source_reference, from, to, type).

    Re-sending the identical claim from the SAME source updates the existing
    row's evidence/confidence/updated_at in place rather than duplicating it
    — this is what lets a sync job re-run safely. A DIFFERENT source making
    the same claim about the same pair gets its OWN row; see the module note
    in app/models.py for why that is deliberate, not a bug to dedupe away.
    """
    existing = db.scalar(
        select(EntityRelationship).where(
            EntityRelationship.source == source,
            EntityRelationship.source_reference == source_reference,
            EntityRelationship.from_entity_id == from_entity_id,
            EntityRelationship.to_entity_id == to_entity_id,
            EntityRelationship.relationship_type == relationship_type,
        )
    )
    if existing is not None:
        existing.evidence = evidence
        existing.confidence = confidence
        db.flush()
        return existing
    row = EntityRelationship(
        source=source,
        source_reference=source_reference,
        relationship_type=relationship_type,
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        evidence=evidence,
        confidence=confidence,
    )
    db.add(row)
    db.flush()
    return row


@dataclass
class DependencyNode:
    """One entity reached during a traversal, and how it was reached."""

    entity_id: int
    entity_type: str
    canonical_name: str
    depth: int
    relationship_type: str
    source: str
    via_entity_id: int
    #: The full chain of entity ids from the root to this node, root first —
    #: what a UI or a later correlation phase would draw as the path.
    path: list[int] = field(default_factory=list)


@dataclass
class TraversalResult:
    root_entity_id: int
    direction: Literal["upstream", "downstream"]
    nodes: list[DependencyNode]
    max_depth: int
    #: True if the traversal stopped at least one branch because it hit
    #: max_depth with more graph beyond it — i.e. the result may be
    #: incomplete, not because of a cycle, but because of the depth cap.
    truncated: bool
    #: True if a cycle was actually encountered (a node reachable by more
    #: than one path, or a path that leads back to an ancestor) — distinct
    #: from truncated: a cyclic graph can still be fully explored within
    #: max_depth, since a cycle only stops re-EXPANSION of an already-visited
    #: node, not the traversal as a whole.
    cycle_detected: bool


def _neighbors(
    db: Session, entity_id: int, direction: Literal["upstream", "downstream"],
) -> list[tuple[EntityRelationship, int]]:
    """One hop's worth of (relationship, neighbor_entity_id) from entity_id.

    This is the single place the DEPENDENCY_TYPES / IMPACT_TYPES direction
    rule from the module docstring is applied.
    """
    out: list[tuple[EntityRelationship, int]] = []
    if direction == "upstream":
        # What entity_id depends on: DEPENDENCY_TYPES forward (entity_id is
        # `from`), IMPACT_TYPES backward (entity_id is `to`, something
        # upstream affects/provides to it).
        for rel in db.scalars(
            select(EntityRelationship).where(
                EntityRelationship.from_entity_id == entity_id,
                EntityRelationship.relationship_type.in_(DEPENDENCY_TYPES),
            )
        ).all():
            out.append((rel, rel.to_entity_id))
        for rel in db.scalars(
            select(EntityRelationship).where(
                EntityRelationship.to_entity_id == entity_id,
                EntityRelationship.relationship_type.in_(IMPACT_TYPES),
            )
        ).all():
            out.append((rel, rel.from_entity_id))
    else:
        # What depends on entity_id (downstream impact): DEPENDENCY_TYPES
        # backward (entity_id is `to`, something needs it), IMPACT_TYPES
        # forward (entity_id is `from`, its failure affects/provides onward).
        for rel in db.scalars(
            select(EntityRelationship).where(
                EntityRelationship.to_entity_id == entity_id,
                EntityRelationship.relationship_type.in_(DEPENDENCY_TYPES),
            )
        ).all():
            out.append((rel, rel.from_entity_id))
        for rel in db.scalars(
            select(EntityRelationship).where(
                EntityRelationship.from_entity_id == entity_id,
                EntityRelationship.relationship_type.in_(IMPACT_TYPES),
            )
        ).all():
            out.append((rel, rel.to_entity_id))
    return out


def traverse(
    db: Session,
    entity_id: int,
    *,
    direction: Literal["upstream", "downstream"],
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> TraversalResult:
    """Breadth-first walk from ``entity_id``, cycle-safe, depth-bounded."""
    max_depth = max(1, min(max_depth, MAX_ALLOWED_DEPTH))
    visited: set[int] = {entity_id}
    nodes: list[DependencyNode] = []
    truncated = False
    cycle_detected = False

    # (entity_id, depth, path-so-far)
    frontier: list[tuple[int, int, list[int]]] = [(entity_id, 0, [entity_id])]
    while frontier:
        current_id, depth, path = frontier.pop(0)
        if depth >= max_depth:
            # There may be more graph beyond this node; we simply don't look.
            if _neighbors(db, current_id, direction):
                truncated = True
            continue
        for rel, neighbor_id in _neighbors(db, current_id, direction):
            if neighbor_id in visited:
                # Reached an already-visited node by a second path, or back
                # to an ancestor on this same path — either way, a cycle.
                cycle_detected = True
                continue
            visited.add(neighbor_id)
            entity = db.get(CanonicalEntity, neighbor_id)
            if entity is None:
                continue  # a relationship pointing at a since-deleted entity
            new_path = path + [neighbor_id]
            nodes.append(DependencyNode(
                entity_id=neighbor_id,
                entity_type=entity.entity_type.value,
                canonical_name=entity.canonical_name,
                depth=depth + 1,
                relationship_type=rel.relationship_type.value,
                source=rel.source.value,
                via_entity_id=current_id,
                path=new_path,
            ))
            frontier.append((neighbor_id, depth + 1, new_path))

    return TraversalResult(
        root_entity_id=entity_id, direction=direction, nodes=nodes,
        max_depth=max_depth, truncated=truncated, cycle_detected=cycle_detected,
    )


def find_path(
    db: Session, from_entity_id: int, to_entity_id: int, *, max_depth: int = MAX_ALLOWED_DEPTH,
) -> list[DependencyNode] | None:
    """Shortest upstream path from ``from_entity_id`` to ``to_entity_id``,
    or ``None`` if none exists within ``max_depth``. Same cycle protection
    as :func:`traverse` (BFS naturally finds the shortest path first).
    """
    max_depth = max(1, min(max_depth, MAX_ALLOWED_DEPTH))
    if from_entity_id == to_entity_id:
        return []
    visited: set[int] = {from_entity_id}
    frontier: list[tuple[int, int, list[DependencyNode]]] = [(from_entity_id, 0, [])]
    while frontier:
        current_id, depth, chain = frontier.pop(0)
        if depth >= max_depth:
            continue
        for rel, neighbor_id in _neighbors(db, current_id, "upstream"):
            if neighbor_id in visited:
                continue
            entity = db.get(CanonicalEntity, neighbor_id)
            if entity is None:
                continue
            node = DependencyNode(
                entity_id=neighbor_id,
                entity_type=entity.entity_type.value,
                canonical_name=entity.canonical_name,
                depth=depth + 1,
                relationship_type=rel.relationship_type.value,
                source=rel.source.value,
                via_entity_id=current_id,
                path=[],
            )
            new_chain = chain + [node]
            if neighbor_id == to_entity_id:
                return new_chain
            visited.add(neighbor_id)
            frontier.append((neighbor_id, depth + 1, new_chain))
    return None


def direct_relationships(db: Session, entity_id: int) -> dict[str, list[dict]]:
    """One-hop view of an entity: what it depends on, and what depends on it.

    Unlike :func:`traverse`, this returns every relationship AS STORED (both
    families, un-collapsed), so a caller can see the raw edges — including
    more than one source's claim about the same pair — before any traversal
    direction logic is applied.
    """
    outgoing = db.scalars(
        select(EntityRelationship).where(EntityRelationship.from_entity_id == entity_id)
    ).all()
    incoming = db.scalars(
        select(EntityRelationship).where(EntityRelationship.to_entity_id == entity_id)
    ).all()

    def _describe(rel: EntityRelationship, other_id: int) -> dict:
        other = db.get(CanonicalEntity, other_id)
        return {
            "relationship_id": rel.id,
            "entity_id": other_id,
            "entity_type": other.entity_type.value if other else None,
            "canonical_name": other.canonical_name if other else None,
            "relationship_type": rel.relationship_type.value,
            "source": rel.source.value,
            "evidence": rel.evidence,
            "confidence": rel.confidence,
        }

    return {
        "outgoing": [_describe(r, r.to_entity_id) for r in outgoing],
        "incoming": [_describe(r, r.from_entity_id) for r in incoming],
    }
