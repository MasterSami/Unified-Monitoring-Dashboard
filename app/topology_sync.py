"""Topology sources — Correlation Phase 3.

Populates :class:`~app.models.EntityRelationship` rows from data this
codebase can actually observe today. Per the phase's "do not blindly merge
relationships" rule, each source writes its own rows (see
``app.dependency_graph.record_relationship``'s upsert key) — this module
never overwrites what a different source or a human declared.

Two sources are wired up here, both by reading the EXISTING
:class:`~app.models.TopologyNode`/:class:`~app.models.TopologyEdge` tables
that ``app.topology`` already collects from live systems, rather than
querying Dynatrace/NNMi a second time:

- ``dynatrace`` — Dynatrace's own resolved service-to-service call graph
  (``TopologyEdge`` rows with ``kind == "path"``) becomes ``CALLS``
  relationships between ``service`` entities.
- ``monitoring`` — NNMi's L2 network connections (``TopologyEdge`` rows with
  ``kind == "l2"``) become ``CONNECTS_TO`` relationships between
  ``network_device`` entities.

The other three sources the phase asks for (``explicit_config``, ``cmdb``,
``manual``) have no automated feed in this environment — there is no CMDB
API configured anywhere in this codebase (see the Phase 0 discovery notes)
and no relationship-config file convention yet. They are fully supported by
the schema and the ``POST /api/v1/topology/relationships`` endpoint; a human
or a script populates them, same as Phase 1's manual entity mappings.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity_resolution import resolve_hosts_batch
from app.models import (
    EntityRelationship,
    EntityType,
    RelationshipType,
    SourcePlatform,
    TopologyEdge,
    TopologyNode,
    TopologySource,
)

logger = logging.getLogger("topology_sync")

#: Fixed per-source confidence — a constant judgement of how much to trust a
#: source's claims by default, not something derived from the data. Kept
#: alongside app.entity_resolution.CONFIDENCE's convention.
DEFAULT_CONFIDENCE: dict[TopologySource, float] = {
    TopologySource.explicit_config: 0.95,
    TopologySource.cmdb: 0.9,
    TopologySource.dynatrace: 0.85,
    TopologySource.monitoring: 0.75,
    TopologySource.manual: 1.0,
}

#: app/topology.py writes Dynatrace call-path edges with kind="path" (a
#: resolved service-to-service path, possibly via middleware — see its
#: attributes for the hop chain, not modeled as separate graph edges here).
_DYNATRACE_EDGE_KINDS = ("path",)


def _resolve_nodes_batch(
    db: Session,
    nodes: list[TopologyNode],
    *,
    platform: SourcePlatform,
    entity_type: EntityType,
) -> dict[tuple[str, str], int | None]:
    """Resolve every distinct TopologyNode to a CanonicalEntity id, one
    prefetched batch per source instance.

    Reuses app.entity_resolution's generic resolver (Phase 1) — it is not
    host-specific despite the name; only its priority chain (manual mapping
    > CMDB > IP > FQDN > hostname > alias > prior resolution) matters here,
    and a topology node has no IP, so it resolves by hostname/alias, same as
    any host with no IP would. Resolving node-by-node (each call rebuilding
    its own lookup context) cost half a dozen queries per node — tens of
    thousands per sync on a real estate; the batch resolver prefetches once.
    """
    by_instance: dict[str, dict[str, TopologyNode]] = {}
    for n in nodes:
        by_instance.setdefault(n.source_instance, {}).setdefault(n.external_id, n)
    out: dict[tuple[str, str], int | None] = {}
    for instance, group in by_instance.items():
        items = [
            {
                "external_id": n.external_id,
                "hostname": n.name or n.external_id,
                "entity_type": entity_type,
            }
            for n in group.values()
        ]
        for external_id, result in resolve_hosts_batch(
            db, platform=platform, instance=instance, items=items,
        ).items():
            out[(instance, external_id)] = result.entity_id
    return out


def _sync_edges(
    db: Session,
    *,
    platform: SourcePlatform,
    edge_kinds: tuple[str, ...],
    entity_type: EntityType,
    source: TopologySource,
    relationship_type: RelationshipType,
    reference_prefix: str,
    evidence,
) -> int:
    """Shared body of the two automated sources: load the platform's edges
    and nodes, resolve the referenced nodes in one batch, then upsert one
    relationship per edge against a single prefetched map of what this
    source already recorded — the same (source, source_reference, from, to,
    type) key ``app.dependency_graph.record_relationship`` uses, without
    its one-SELECT-per-edge cost.
    """
    edges = db.scalars(
        select(TopologyEdge).where(
            TopologyEdge.source_platform == platform,
            TopologyEdge.kind.in_(edge_kinds),
        )
    ).all()
    if not edges:
        return 0

    nodes_by_key: dict[tuple[str, str], TopologyNode] = {
        (n.source_instance, n.external_id): n
        for n in db.scalars(
            select(TopologyNode).where(TopologyNode.source_platform == platform)
        ).all()
    }
    referenced: list[TopologyNode] = []
    for edge in edges:
        for ext in (edge.from_external_id, edge.to_external_id):
            node = nodes_by_key.get((edge.source_instance, ext))
            if node is not None:
                referenced.append(node)
    entity_ids = _resolve_nodes_batch(db, referenced, platform=platform, entity_type=entity_type)

    existing: dict[tuple[str, int, int, RelationshipType], EntityRelationship] = {
        (r.source_reference, r.from_entity_id, r.to_entity_id, r.relationship_type): r
        for r in db.scalars(
            select(EntityRelationship).where(EntityRelationship.source == source)
        ).all()
    }
    confidence = DEFAULT_CONFIDENCE[source]
    written = 0
    for edge in edges:
        from_node = nodes_by_key.get((edge.source_instance, edge.from_external_id))
        to_node = nodes_by_key.get((edge.source_instance, edge.to_external_id))
        if from_node is None or to_node is None:
            continue  # edge references a node this sync can't see — skip, don't guess
        from_id = entity_ids.get((edge.source_instance, from_node.external_id))
        to_id = entity_ids.get((edge.source_instance, to_node.external_id))
        if from_id is None or to_id is None or from_id == to_id:
            continue
        reference = f"{reference_prefix}:{edge.source_instance}:{edge.external_id}"
        text = evidence(from_node, to_node, edge)
        key = (reference, from_id, to_id, relationship_type)
        row = existing.get(key)
        if row is not None:
            row.evidence = text
            row.confidence = confidence
        else:
            row = EntityRelationship(
                source=source,
                source_reference=reference,
                relationship_type=relationship_type,
                from_entity_id=from_id,
                to_entity_id=to_id,
                evidence=text,
                confidence=confidence,
            )
            db.add(row)
            existing[key] = row
        written += 1
    db.flush()
    return written


def sync_dynatrace_relationships(db: Session) -> int:
    """Turn Dynatrace's collected service call graph into CALLS relationships.

    Returns the number of relationships created or updated.
    """
    written = _sync_edges(
        db,
        platform=SourcePlatform.dynatrace,
        edge_kinds=_DYNATRACE_EDGE_KINDS,
        entity_type=EntityType.service,
        source=TopologySource.dynatrace,
        relationship_type=RelationshipType.calls,
        reference_prefix="dynatrace",
        evidence=lambda f, t, e: (
            f"Dynatrace: {f.name} calls {t.name}" + (f" ({e.label})" if e.label else "")
        ),
    )
    logger.info("dynatrace topology sync: %d relationships", written)
    return written


def sync_nnmi_relationships(db: Session) -> int:
    """Turn NNMi's collected L2 network connections into CONNECTS_TO
    relationships between network_device entities.

    Returns the number of relationships created or updated.
    """
    written = _sync_edges(
        db,
        platform=SourcePlatform.nnmi,
        edge_kinds=("l2",),
        entity_type=EntityType.network_device,
        source=TopologySource.monitoring,
        relationship_type=RelationshipType.connects_to,
        reference_prefix="nnmi",
        evidence=lambda f, t, e: f"NNMi L2 connection: {f.name} - {t.name}",
    )
    logger.info("NNMi topology sync: %d relationships", written)
    return written


def sync_all(db: Session) -> dict[str, int]:
    """Run every automated source. Best-effort per source — one failing
    never blocks the other (same fault-isolation philosophy as
    BaseCollector.run()).
    """
    result: dict[str, int] = {}
    for name, fn in (
        ("dynatrace", sync_dynatrace_relationships),
        ("monitoring", sync_nnmi_relationships),
    ):
        try:
            result[name] = fn(db)
        except Exception as exc:  # noqa: BLE001 — contained by design
            logger.exception("topology sync (%s) failed: %s", name, exc)
            result[name] = 0
    return result
