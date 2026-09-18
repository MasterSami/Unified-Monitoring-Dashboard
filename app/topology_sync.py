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

from app.dependency_graph import record_relationship
from app.entity_resolution import resolve_host_entity
from app.models import EntityType, RelationshipType, SourcePlatform, TopologyEdge, TopologyNode, TopologySource

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


def _resolve_topology_node(
    db: Session,
    node: TopologyNode,
    *,
    platform: SourcePlatform,
    entity_type: EntityType,
    cache: dict[tuple[str, str], int],
) -> int | None:
    """Resolve one TopologyNode to a CanonicalEntity id, cached per sync run.

    Reuses app.entity_resolution's generic resolver (Phase 1) — it is not
    host-specific despite the name; only its priority chain (manual mapping
    > CMDB > IP > FQDN > hostname > alias > prior resolution) matters here,
    and a topology node has no IP, so it resolves by hostname/alias, same as
    any host with no IP would.
    """
    key = (node.source_instance, node.external_id)
    if key in cache:
        return cache[key]
    result = resolve_host_entity(
        db, platform=platform, instance=node.source_instance,
        external_id=node.external_id, hostname=node.name or node.external_id,
        entity_type=entity_type,
    )
    cache[key] = result.entity_id
    return result.entity_id


def sync_dynatrace_relationships(db: Session) -> int:
    """Turn Dynatrace's collected service call graph into CALLS relationships.

    Returns the number of relationships created or updated.
    """
    edges = db.scalars(
        select(TopologyEdge).where(
            TopologyEdge.source_platform == SourcePlatform.dynatrace,
            TopologyEdge.kind.in_(_DYNATRACE_EDGE_KINDS),
        )
    ).all()
    if not edges:
        return 0

    nodes_by_key: dict[tuple[str, str], TopologyNode] = {
        (n.source_instance, n.external_id): n
        for n in db.scalars(
            select(TopologyNode).where(TopologyNode.source_platform == SourcePlatform.dynatrace)
        ).all()
    }
    cache: dict[tuple[str, str], int] = {}
    written = 0
    for edge in edges:
        from_node = nodes_by_key.get((edge.source_instance, edge.from_external_id))
        to_node = nodes_by_key.get((edge.source_instance, edge.to_external_id))
        if from_node is None or to_node is None:
            continue  # edge references a node this sync can't see — skip, don't guess
        from_id = _resolve_topology_node(
            db, from_node, platform=SourcePlatform.dynatrace,
            entity_type=EntityType.service, cache=cache,
        )
        to_id = _resolve_topology_node(
            db, to_node, platform=SourcePlatform.dynatrace,
            entity_type=EntityType.service, cache=cache,
        )
        if from_id is None or to_id is None or from_id == to_id:
            continue
        record_relationship(
            db,
            source=TopologySource.dynatrace,
            relationship_type=RelationshipType.calls,
            from_entity_id=from_id,
            to_entity_id=to_id,
            source_reference=f"dynatrace:{edge.source_instance}:{edge.external_id}",
            evidence=f"Dynatrace: {from_node.name} calls {to_node.name}"
                     + (f" ({edge.label})" if edge.label else ""),
            confidence=DEFAULT_CONFIDENCE[TopologySource.dynatrace],
        )
        written += 1
    logger.info("dynatrace topology sync: %d relationships", written)
    return written


def sync_nnmi_relationships(db: Session) -> int:
    """Turn NNMi's collected L2 network connections into CONNECTS_TO
    relationships between network_device entities.

    Returns the number of relationships created or updated.
    """
    edges = db.scalars(
        select(TopologyEdge).where(
            TopologyEdge.source_platform == SourcePlatform.nnmi,
            TopologyEdge.kind == "l2",
        )
    ).all()
    if not edges:
        return 0

    nodes_by_key: dict[tuple[str, str], TopologyNode] = {
        (n.source_instance, n.external_id): n
        for n in db.scalars(
            select(TopologyNode).where(TopologyNode.source_platform == SourcePlatform.nnmi)
        ).all()
    }
    cache: dict[tuple[str, str], int] = {}
    written = 0
    for edge in edges:
        from_node = nodes_by_key.get((edge.source_instance, edge.from_external_id))
        to_node = nodes_by_key.get((edge.source_instance, edge.to_external_id))
        if from_node is None or to_node is None:
            continue
        from_id = _resolve_topology_node(
            db, from_node, platform=SourcePlatform.nnmi,
            entity_type=EntityType.network_device, cache=cache,
        )
        to_id = _resolve_topology_node(
            db, to_node, platform=SourcePlatform.nnmi,
            entity_type=EntityType.network_device, cache=cache,
        )
        if from_id is None or to_id is None or from_id == to_id:
            continue
        record_relationship(
            db,
            source=TopologySource.monitoring,
            relationship_type=RelationshipType.connects_to,
            from_entity_id=from_id,
            to_entity_id=to_id,
            source_reference=f"nnmi:{edge.source_instance}:{edge.external_id}",
            evidence=f"NNMi L2 connection: {from_node.name} - {to_node.name}",
            confidence=DEFAULT_CONFIDENCE[TopologySource.monitoring],
        )
        written += 1
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
