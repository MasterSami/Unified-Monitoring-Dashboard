"""JSON API under ``/api/v1``.

These endpoints back the UI and are the integration surface for later reports
and automation.
"""

from __future__ import annotations

import csv
import io
import secrets
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app.config import Settings, get_settings
from app.db import get_db
from app.dependency_graph import direct_relationships, find_path, record_relationship, traverse
from app.entity_resolution import create_manual_mapping
from app.models import (
    PLATFORM_ORDER,
    Alert,
    CanonicalEntity,
    CollectorRun,
    EntityAlias,
    EntityIP,
    EntityManualMapping,
    EntityRelationship,
    EntityType,
    Host,
    HostStatus,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    RunStatus,
    SourcePlatform,
    TopologyEdge,
    TopologyNode,
    TopologySource,
)
from app.normalizer import severity_label
from app.scheduler import (
    get_collector_statuses,
    get_service,
    request_forecast_run,
    request_run_all,
    request_run_one,
    request_topology_run,
    run_forecast_now,
    run_topology_now,
)
from app.sitescope_ingest import ingest_lines
from app.topology import (
    NNMI_L2_COLUMNS,
    UNIFIED_COLUMNS,
    dynatrace_app_rows,
    dynatrace_service_rows,
    dynatrace_unified_rows,
    nnmi_connection_rows,
)
from app.topology_sync import sync_all
from app.schemas import (
    AlertOut,
    CollectorStatus,
    DependencyNodeOut,
    DirectRelationshipsOut,
    EntityMappingIn,
    EntityMappingOut,
    EntityOut,
    EntitySourceRef,
    EventOut,
    HostOut,
    IngestResult,
    LogicalEventOccurrenceOut,
    LogicalEventOut,
    PathOut,
    PlatformHostCount,
    RelatedEntityOut,
    RelationshipIn,
    RelationshipOut,
    SeverityBucket,
    SiteScopeIngest,
    SummaryOut,
    TraversalOut,
)

router = APIRouter(prefix="/api/v1", tags=["api"])


# --- SiteScope push ingest --------------------------------------------------


def _check_ingest_auth(authorization: str | None, settings: Settings) -> None:
    """Bearer-token auth for the ingest endpoint (constant-time compare)."""
    token = settings.sitescope_ingest_token
    if not token:
        raise HTTPException(status_code=503, detail="SiteScope ingest is not configured")
    expected = f"Bearer {token}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@router.post("/ingest/sitescope", response_model=IngestResult)
def ingest_sitescope(
    payload: SiteScopeIngest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IngestResult:
    """Receive a batch of redacted SiteScope log lines from the forwarder.

    Bearer-authenticated, size-capped, and idempotent: re-sending the same batch
    updates rows in place (no duplicates). A heartbeat (empty ``lines``) still
    records a collector run so a dead forwarder shows up as stale in the UI.
    """
    _check_ingest_auth(authorization, settings)
    if len(payload.lines) > settings.ingest_max_events:
        raise HTTPException(
            status_code=413,
            detail=f"too many events (>{settings.ingest_max_events})",
        )
    if sum(len(line) for line in payload.lines) > settings.ingest_max_bytes:
        raise HTTPException(status_code=413, detail="payload too large")

    started = datetime.now(timezone.utc)
    c = ingest_lines(db, payload.source_instance, payload.lines)

    # Collector-health heartbeat: one run row per ingest, so the dashboard can
    # tell "no alerts" (recent run, 0 events) from "collector dead" (stale run).
    db.add(
        CollectorRun(
            platform="sitescope",
            instance=payload.source_instance,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            status=RunStatus.success,
            items_collected=c.events,
            # Distinct hosts seen this batch (not just new inserts) so the
            # collector row shows the real fleet size, not 0 on repeat runs.
            hosts_collected=c.hosts,
            alerts_collected=c.events,
        )
    )
    db.commit()
    return IngestResult(
        status="ok",
        received=c.received,
        inserted=c.inserted,
        updated=c.updated,
        skipped=c.skipped,
        redactions=c.redactions,
    )


def _page_window(
    limit: int | None, offset: int | None, settings: Settings
) -> tuple[int, int]:
    """Clamp a caller's limit/offset to the configured cap.

    These endpoints used to return the whole table — and with ``active=false``
    the alerts one returned the entire 30-day resolved backfill, every row
    validated through Pydantic on the way out.
    """
    size = settings.api_default_limit if limit is None else limit
    return max(1, min(size, settings.api_max_limit)), max(0, offset or 0)


@router.get("/hosts", response_model=list[HostOut])
def list_hosts(
    platform: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[Host]:
    """Return hosts, optionally filtered by platform, status, or search text."""
    stmt = select(Host).options(defer(Host.raw_payload))
    if platform:
        stmt = stmt.where(Host.source_platform == platform)
    if status:
        stmt = stmt.where(Host.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Host.hostname).like(like)
            | func.lower(Host.ip).like(like)
            | func.lower(func.coalesce(Host.ip_all, "")).like(like)
        )
    size, start = _page_window(limit, offset, settings)
    stmt = stmt.order_by(Host.hostname.asc()).offset(start).limit(size)
    return list(db.scalars(stmt).all())


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    active: bool = Query(default=True),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[Alert]:
    """Return alerts. ``active=true`` (default) excludes resolved alerts."""
    stmt = select(Alert).options(defer(Alert.raw_payload))
    if active:
        stmt = stmt.where(Alert.resolved.is_(False))
    size, start = _page_window(limit, offset, settings)
    stmt = (
        stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .offset(start)
        .limit(size)
    )
    return list(db.scalars(stmt).all())


# --- Correlation Phase 1: canonical events + entity resolution --------------


@router.get("/events", response_model=list[EventOut])
def list_events(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    entity_id: int | None = Query(default=None),
    resolved: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[Alert]:
    """Canonical monitoring events (the Alert table's full Phase-1 schema).

    Unlike ``/alerts`` (kept as-is for existing consumers) this returns every
    canonical field: source identity, preserved original severity/
    description, resolved entity, and whatever a source's adapter filled in.
    ``resolved`` omitted returns both; ``true``/``false`` filters to one.
    """
    stmt = select(Alert).options(defer(Alert.raw_payload))
    if platform:
        stmt = stmt.where(Alert.source_platform == platform)
    if instance:
        stmt = stmt.where(Alert.source_instance == instance)
    if entity_id is not None:
        stmt = stmt.where(Alert.entity_id == entity_id)
    if resolved is not None:
        stmt = stmt.where(Alert.resolved.is_(resolved))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Alert.title).like(like)
            | func.lower(func.coalesce(Alert.host_hostname, "")).like(like)
            | func.lower(func.coalesce(Alert.external_id, "")).like(like)
        )
    size, start = _page_window(limit, offset, settings)
    stmt = (
        stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .offset(start)
        .limit(size)
    )
    return list(db.scalars(stmt).all())


def _entity_out(
    db: Session, entity: CanonicalEntity, *, with_sources: bool
) -> EntityOut:
    aliases = list(
        db.scalars(
            select(EntityAlias.alias).where(EntityAlias.entity_id == entity.id)
        ).all()
    )
    ips = list(
        db.scalars(select(EntityIP.ip).where(EntityIP.entity_id == entity.id)).all()
    )
    sources: list[EntitySourceRef] = []
    if with_sources:
        for h in db.scalars(select(Host).where(Host.entity_id == entity.id)).all():
            sources.append(
                EntitySourceRef(
                    source_platform=h.source_platform.value,
                    source_instance=h.source_instance,
                    external_id=h.external_id,
                    hostname=h.hostname,
                    resolution_method=h.resolution_method,
                    resolution_confidence=h.resolution_confidence,
                )
            )
    return EntityOut(
        entity_id=entity.id,
        entity_type=entity.entity_type.value,
        canonical_name=entity.canonical_name,
        cmdb_id=entity.cmdb_id,
        aliases=aliases,
        ips=ips,
        source_entities=sources,
    )


@router.get("/entities", response_model=list[EntityOut])
def list_entities(
    entity_type: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[EntityOut]:
    """Resolved canonical entities. ``q`` matches the name, an alias, or an IP."""
    stmt = select(CanonicalEntity)
    if entity_type:
        stmt = stmt.where(CanonicalEntity.entity_type == entity_type)
    if q:
        like = f"%{q.lower()}%"
        matching_ids = set(
            db.scalars(
                select(EntityAlias.entity_id).where(
                    func.lower(EntityAlias.alias).like(like)
                )
            ).all()
        ) | set(
            db.scalars(
                select(EntityIP.entity_id).where(func.lower(EntityIP.ip).like(like))
            ).all()
        )
        cond = func.lower(CanonicalEntity.canonical_name).like(like)
        if matching_ids:
            cond = cond | CanonicalEntity.id.in_(matching_ids)
        stmt = stmt.where(cond)
    size, start = _page_window(limit, offset, settings)
    stmt = stmt.order_by(CanonicalEntity.id.asc()).offset(start).limit(size)
    entities = list(db.scalars(stmt).all())
    return [_entity_out(db, e, with_sources=False) for e in entities]


@router.get("/entities/{entity_id}", response_model=EntityOut)
def get_entity(entity_id: int, db: Session = Depends(get_db)) -> EntityOut:
    """One entity's full detail: aliases, IPs, and every source row resolved
    to it (with the method/confidence each one was resolved by).
    """
    entity = db.get(CanonicalEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return _entity_out(db, entity, with_sources=True)


@router.get("/entity-mappings", response_model=list[EntityMappingOut])
def list_entity_mappings(
    entity_id: int | None = Query(default=None),
    platform: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[EntityMappingOut]:
    """Every known ``source identifier -> entity`` mapping.

    Combines admin-declared manual mappings with every automatically
    resolved Host row that has an entity — the same union a later
    correlation phase would need to answer "why is this entity what it is".
    """
    size, start = _page_window(limit, offset, settings)
    out: list[EntityMappingOut] = []

    manual_stmt = select(EntityManualMapping)
    if entity_id is not None:
        manual_stmt = manual_stmt.where(EntityManualMapping.entity_id == entity_id)
    if platform:
        manual_stmt = manual_stmt.where(EntityManualMapping.source_platform == platform)
    for m in db.scalars(manual_stmt).all():
        out.append(
            EntityMappingOut(
                source_platform=m.source_platform.value,
                source_instance=m.source_instance,
                source_identifier=m.source_identifier,
                hostname=None,
                entity_id=m.entity_id,
                resolution_method="manual_mapping",
                resolution_confidence=1.0,
                is_manual=True,
            )
        )

    host_stmt = select(Host).where(Host.entity_id.isnot(None))
    if entity_id is not None:
        host_stmt = host_stmt.where(Host.entity_id == entity_id)
    if platform:
        host_stmt = host_stmt.where(Host.source_platform == platform)
    for h in db.scalars(host_stmt).all():
        out.append(
            EntityMappingOut(
                source_platform=h.source_platform.value,
                source_instance=h.source_instance,
                source_identifier=h.external_id,
                hostname=h.hostname,
                entity_id=h.entity_id,  # type: ignore[arg-type]
                resolution_method=h.resolution_method or "unknown",
                resolution_confidence=h.resolution_confidence,
                is_manual=False,
            )
        )
    return out[start : start + size]


@router.post("/entity-mappings", response_model=EntityMappingOut, status_code=201)
def create_entity_mapping(
    payload: EntityMappingIn, db: Session = Depends(get_db)
) -> EntityMappingOut:
    """Declare an explicit source-identifier-to-entity mapping.

    Checked first, ahead of every automatic resolution method — see
    app/entity_resolution.py. Pass ``entity_id`` to attach to an existing
    entity, or omit it (with ``entity_type``/``canonical_name``) to create a
    new one, e.g. to pre-declare an identity before the source has reported
    it at all.
    """
    try:
        platform = SourcePlatform(payload.source_platform)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown source_platform") from None

    entity_id = payload.entity_id
    if entity_id is None:
        try:
            entity_type = EntityType(payload.entity_type)
        except ValueError:
            raise HTTPException(status_code=422, detail="unknown entity_type") from None
        entity = CanonicalEntity(
            entity_type=entity_type,
            canonical_name=payload.canonical_name or payload.source_identifier,
        )
        db.add(entity)
        db.flush()
        entity_id = entity.id
    elif db.get(CanonicalEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")

    mapping = create_manual_mapping(
        db,
        entity_id=entity_id,
        source_platform=platform,
        source_instance=payload.source_instance,
        source_identifier=payload.source_identifier,
        note=payload.note,
    )
    db.commit()
    return EntityMappingOut(
        source_platform=mapping.source_platform.value,
        source_instance=mapping.source_instance,
        source_identifier=mapping.source_identifier,
        hostname=None,
        entity_id=mapping.entity_id,
        resolution_method="manual_mapping",
        resolution_confidence=1.0,
        is_manual=True,
    )


# --- Correlation Phase 2: deduplicated logical events -----------------------


@router.get("/logical-events", response_model=list[LogicalEventOut])
def list_logical_events(
    status: str | None = Query(default=None),
    entity_id: int | None = Query(default=None),
    platform: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[LogicalEvent]:
    """Deduplicated logical events (see app/dedup.py) — each groups occurrences
    that share one fingerprint (same resolved entity + normalized problem
    type), never events that merely share a host, a time window, or an
    application. ``platform`` filters to groups with at least one occurrence
    from that source.
    """
    stmt = select(LogicalEvent)
    if status:
        stmt = stmt.where(LogicalEvent.status == status)
    if entity_id is not None:
        stmt = stmt.where(LogicalEvent.entity_id == entity_id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(LogicalEvent.title).like(like)
            | func.lower(LogicalEvent.normalized_problem_type).like(like)
        )
    stmt = stmt.order_by(LogicalEvent.last_seen.desc().nullslast())
    size, start = _page_window(limit, offset, settings)
    if platform:
        # sources is a JSON list — filtering it portably across SQLite and
        # PostgreSQL needs Python, so paginate after filtering rather than in
        # SQL. LogicalEvent rows are one per distinct (entity, problem type),
        # far fewer than raw events, so this stays cheap.
        rows = [r for r in db.scalars(stmt).all() if platform in (r.sources or [])]
        return rows[start : start + size]
    stmt = stmt.offset(start).limit(size)
    return list(db.scalars(stmt).all())


@router.get("/logical-events/{logical_event_id}", response_model=LogicalEventOut)
def get_logical_event(logical_event_id: int, db: Session = Depends(get_db)) -> LogicalEvent:
    le = db.get(LogicalEvent, logical_event_id)
    if le is None:
        raise HTTPException(status_code=404, detail="logical event not found")
    return le


@router.get(
    "/logical-events/{logical_event_id}/occurrences",
    response_model=list[LogicalEventOccurrenceOut],
)
def get_logical_event_occurrences(
    logical_event_id: int, db: Session = Depends(get_db)
) -> list[Alert]:
    """Every original source occurrence folded into this logical event.

    None are merged or deleted — each keeps its own source, source_event_id
    (``external_id``), and original_severity, exactly as first reported.
    """
    if db.get(LogicalEvent, logical_event_id) is None:
        raise HTTPException(status_code=404, detail="logical event not found")
    stmt = (
        select(Alert)
        .options(defer(Alert.raw_payload))
        .where(Alert.logical_event_id == logical_event_id)
        .order_by(Alert.started_at.desc().nullslast())
    )
    return list(db.scalars(stmt).all())


# --- Correlation Phase 3: dependency graph -----------------------------------
#
# A SEPARATE surface from the existing /topology/graph (raw per-platform
# NNMi/Dynatrace topology) — this one is the resolved-entity graph. See the
# module note above EntityRelationship in app/models.py.


@router.get("/topology/relationships", response_model=list[RelationshipOut])
def list_relationships(
    from_entity_id: int | None = Query(default=None),
    to_entity_id: int | None = Query(default=None),
    entity_id: int | None = Query(default=None, description="Either direction"),
    relationship_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[EntityRelationship]:
    """Every stored relationship, as-is — every source's own claim, never
    collapsed into one "true" answer.
    """
    stmt = select(EntityRelationship)
    if from_entity_id is not None:
        stmt = stmt.where(EntityRelationship.from_entity_id == from_entity_id)
    if to_entity_id is not None:
        stmt = stmt.where(EntityRelationship.to_entity_id == to_entity_id)
    if entity_id is not None:
        stmt = stmt.where(
            (EntityRelationship.from_entity_id == entity_id)
            | (EntityRelationship.to_entity_id == entity_id)
        )
    if relationship_type:
        stmt = stmt.where(EntityRelationship.relationship_type == relationship_type)
    if source:
        stmt = stmt.where(EntityRelationship.source == source)
    size, start = _page_window(limit, offset, settings)
    stmt = stmt.order_by(EntityRelationship.id.asc()).offset(start).limit(size)
    return list(db.scalars(stmt).all())


@router.post("/topology/relationships", response_model=RelationshipOut, status_code=201)
def create_relationship(
    payload: RelationshipIn, db: Session = Depends(get_db)
) -> EntityRelationship:
    """Declare a relationship between two existing canonical entities.

    For ``manual``/``cmdb``/``explicit_config`` claims — see the module note
    in app/topology_sync.py for why those three have no automated feed here.
    """
    try:
        source = TopologySource(payload.source)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown source") from None
    try:
        rel_type = RelationshipType(payload.relationship_type)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown relationship_type") from None
    if db.get(CanonicalEntity, payload.from_entity_id) is None:
        raise HTTPException(status_code=404, detail="from_entity_id not found")
    if db.get(CanonicalEntity, payload.to_entity_id) is None:
        raise HTTPException(status_code=404, detail="to_entity_id not found")
    if payload.from_entity_id == payload.to_entity_id:
        raise HTTPException(status_code=422, detail="an entity cannot relate to itself")

    row = record_relationship(
        db,
        source=source,
        relationship_type=rel_type,
        from_entity_id=payload.from_entity_id,
        to_entity_id=payload.to_entity_id,
        source_reference=payload.source_reference,
        evidence=payload.evidence,
        confidence=payload.confidence,
    )
    db.commit()
    return row


@router.post("/topology/sync")
def run_topology_sync(db: Session = Depends(get_db)) -> dict[str, int]:
    """Re-derive dynatrace/monitoring relationships from the topology tables
    app.topology already collects. Safe to re-run — see record_relationship's
    upsert semantics.
    """
    result = sync_all(db)
    db.commit()
    return result


@router.get("/topology/entities/{entity_id}", response_model=DirectRelationshipsOut)
def get_entity_relationships(
    entity_id: int, db: Session = Depends(get_db)
) -> DirectRelationshipsOut:
    """One entity's immediate neighbors, both directions, every source."""
    if db.get(CanonicalEntity, entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")
    rels = direct_relationships(db, entity_id)
    return DirectRelationshipsOut(
        entity_id=entity_id,
        outgoing=[RelatedEntityOut(**r) for r in rels["outgoing"]],
        incoming=[RelatedEntityOut(**r) for r in rels["incoming"]],
    )


def _active_issue_entity_ids(db: Session, entity_ids: set[int]) -> set[int]:
    """Which of these entities have an open/deduplicated/reopened LogicalEvent
    right now — real Phase 2 data, one batched query, not a per-node lookup.
    """
    if not entity_ids:
        return set()
    rows = db.scalars(
        select(LogicalEvent.entity_id).where(
            LogicalEvent.entity_id.in_(entity_ids),
            LogicalEvent.status.in_((
                LogicalEventStatus.open, LogicalEventStatus.deduplicated,
                LogicalEventStatus.reopened,
            )),
        )
    ).all()
    return {eid for eid in rows if eid is not None}


def _traversal_out(db: Session, entity: CanonicalEntity, result) -> TraversalOut:
    all_ids = {n.entity_id for n in result.nodes} | {entity.id}
    unhealthy = _active_issue_entity_ids(db, all_ids)
    return TraversalOut(
        root_entity_id=result.root_entity_id,
        root_entity_type=entity.entity_type.value,
        root_canonical_name=entity.canonical_name,
        root_has_active_issue=entity.id in unhealthy,
        direction=result.direction,
        nodes=[
            DependencyNodeOut(**vars(n), has_active_issue=n.entity_id in unhealthy)
            for n in result.nodes
        ],
        max_depth=result.max_depth, truncated=result.truncated,
        cycle_detected=result.cycle_detected,
    )


@router.get("/topology/dependencies/{entity_id}", response_model=TraversalOut)
def get_dependencies(
    entity_id: int,
    max_depth: int = Query(default=5, ge=1, le=25),
    db: Session = Depends(get_db),
) -> TraversalOut:
    """What this entity depends on — direct, one-hop, and multi-hop, cycle-safe."""
    entity = db.get(CanonicalEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    result = traverse(db, entity_id, direction="upstream", max_depth=max_depth)
    return _traversal_out(db, entity, result)


@router.get("/topology/impact/{entity_id}", response_model=TraversalOut)
def get_impact(
    entity_id: int,
    max_depth: int = Query(default=5, ge=1, le=25),
    db: Session = Depends(get_db),
) -> TraversalOut:
    """What is affected if this entity fails — direct, one-hop, and
    multi-hop, cycle-safe. Answers "what depends on this host", "which
    applications/APIs depend on this database", "which business services
    are affected" — filter the returned nodes by entity_type client-side.
    """
    entity = db.get(CanonicalEntity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    result = traverse(db, entity_id, direction="downstream", max_depth=max_depth)
    return _traversal_out(db, entity, result)


@router.get("/topology/path", response_model=PathOut)
def get_path(
    from_entity_id: int = Query(...),
    to_entity_id: int = Query(...),
    max_depth: int = Query(default=25, ge=1, le=25),
    db: Session = Depends(get_db),
) -> PathOut:
    """Is ``to_entity_id`` reachable upstream from ``from_entity_id``, and
    how — e.g. is Customer API indirectly dependent on CustomerDB.
    """
    if db.get(CanonicalEntity, from_entity_id) is None:
        raise HTTPException(status_code=404, detail="from_entity_id not found")
    if db.get(CanonicalEntity, to_entity_id) is None:
        raise HTTPException(status_code=404, detail="to_entity_id not found")
    hops = find_path(db, from_entity_id, to_entity_id, max_depth=max_depth)
    return PathOut(
        from_entity_id=from_entity_id, to_entity_id=to_entity_id,
        found=hops is not None,
        hops=[DependencyNodeOut(**vars(n)) for n in (hops or [])],
    )


def _csv_response(
    filename: str, header: list[str], rows: Iterable[Sequence[object]]
) -> StreamingResponse:
    """Stream rows out as a downloadable CSV.

    ``rows`` is consumed lazily, so a large export never exists in memory in
    full. It previously built the entire file into a StringIO and handed back
    ``iter([whole_string])`` — a StreamingResponse in name only, which meant the
    export lived in memory three times over (ORM rows, list of lists, and the
    joined string) before the first byte reached the client.
    """

    def chunks():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            if buf.tell() > 64_000:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        if buf.tell():
            yield buf.getvalue()

    return StreamingResponse(
        chunks(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _require_export(settings: Settings) -> None:
    """Reject the request when CSV export is disabled by config."""
    if not settings.enable_export:
        raise HTTPException(status_code=404, detail="CSV export is disabled")


@router.get("/hosts.csv")
def export_hosts_csv(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Export hosts (respecting filters) as CSV."""
    _require_export(settings)
    stmt = select(Host).options(defer(Host.raw_payload), defer(Host.metrics))
    if platform and platform != "all":
        stmt = stmt.where(Host.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(Host.source_instance == instance)
    if status and status != "all":
        stmt = stmt.where(Host.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Host.hostname).like(like)
            | func.lower(func.coalesce(Host.ip, "")).like(like)
            | func.lower(func.coalesce(Host.group_name, "")).like(like)
            | func.lower(func.coalesce(Host.source_instance, "")).like(like)
        )
    stmt = stmt.order_by(Host.hostname.asc())
    # Generator + unbuffered iteration: rows are written to the response as the
    # cursor yields them, so the whole export never sits in memory at once.
    rows = (
        [
            h.hostname,
            h.ip or "",
            h.source_platform.value,
            h.source_instance,
            h.status.value,
            h.group_name or "",
            h.last_seen.isoformat() if h.last_seen else "",
        ]
        for h in db.scalars(stmt)
    )
    return _csv_response(
        "hosts.csv",
        ["hostname", "ip", "platform", "instance", "status", "group", "last_seen"],
        rows,
    )


@router.get("/capacity.csv")
def export_capacity_csv(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    group: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Export the capacity view (hosts + CPU/mem/disk) as CSV.

    Same filters as the Capacity page, built by the same function, so the
    service / group filter behaves identically here and on screen.
    """
    _require_export(settings)
    from app.routers.pages import _hosts_stmt

    stmt = _hosts_stmt(q, platform, status, instance, group).order_by(
        Host.hostname.asc()
    )
    rows = (
        [
            h.hostname,
            h.ip or "",
            h.source_platform.value,
            h.source_instance,
            h.group_name or "",
            "" if h.cpu_pct is None else h.cpu_pct,
            "" if h.mem_pct is None else h.mem_pct,
            "" if h.disk_pct is None else h.disk_pct,
            (h.metrics or {}).get("cores", ""),
            (h.metrics or {}).get("mem_total_gb", ""),
            h.status.value,
        ]
        for h in db.scalars(stmt)
    )
    return _csv_response(
        "capacity.csv",
        [
            "server",
            "ip",
            "platform",
            "instance",
            "group",
            "cpu_pct",
            "mem_pct",
            "disk_pct",
            "cores",
            "mem_total_gb",
            "status",
        ],
        rows,
    )


@router.get("/alerts.csv")
def export_alerts_csv(
    active: bool = Query(default=True),
    q: str | None = Query(default=None),
    group: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Export alerts (respecting filters) as CSV.

    Built by the same function as the Alerts page so search and the
    service / group filter mean the same thing in both places.
    """
    _require_export(settings)
    from app.routers.pages import _alerts_filtered_stmt

    stmt = _alerts_filtered_stmt(
        q, state="active" if active else "all", group=group
    )
    stmt = stmt.order_by(
        Alert.severity_int.desc(), Alert.started_at.desc().nullslast()
    )
    rows = (
        [
            a.severity_int,
            a.severity_label,
            a.title,
            a.source_platform.value,
            a.source_instance,
            a.host_hostname or "",
            a.started_at.isoformat() if a.started_at else "",
            "resolved" if a.resolved else "active",
        ]
        for a in db.scalars(stmt)
    )
    return _csv_response(
        "alerts.csv",
        [
            "severity_int",
            "severity_label",
            "title",
            "platform",
            "instance",
            "host",
            "started_at",
            "state",
        ],
        rows,
    )


# --- Branded Excel export ---------------------------------------------------

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _fname_stamp(value: str | None) -> str:
    """A filesystem-safe compact stamp from a datetime-local string (or 'all')."""
    if not value:
        return "all"
    return "".join(c for c in value if c.isalnum())


def _period_str(date_from: str | None, date_to: str | None) -> str:
    return f"{date_from or 'earliest'} → {date_to or 'now'}"


def _xlsx_response(name: str, data: bytes) -> Response:
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _zabbix_identity(db: Session) -> tuple[set[str], set[str]]:
    """(IPs, lowercased hostnames) of every Zabbix-monitored host.

    Used to drop duplicates from Dynatrace exports: Zabbix is the primary tool,
    so a Dynatrace host with the same IP or hostname is already covered.
    """
    ips: set[str] = set()
    names: set[str] = set()
    for ip, hostname in db.execute(
        select(Host.ip, Host.hostname).where(Host.source_platform == "zabbix")
    ):
        if ip:
            ips.add(ip)
        if hostname:
            names.add(hostname.strip().lower())
    return ips, names


def _in_zabbix(h: Host, zbx_ips: set[str], zbx_names: set[str]) -> bool:
    return bool(
        (h.ip and h.ip in zbx_ips)
        or (h.hostname and h.hostname.strip().lower() in zbx_names)
    )


def _live_disk_breakdown(settings: Settings) -> dict[str, list]:
    """Per-partition disk for all configured Dynatrace instances (best-effort)."""
    from app.servers import load_servers

    breakdown: dict[str, list] = {}
    if settings.mock_mode:
        return breakdown
    for s in load_servers(settings):
        if getattr(s, "platform", "") != "dynatrace":
            continue
        collector = get_service().get(s.name)
        if collector is None or getattr(collector, "name", "") != "dynatrace":
            continue
        try:
            for hid, disks in collector.disk_breakdown().items():
                breakdown.setdefault(hid, []).extend(disks)
        except Exception:  # noqa: BLE001 — one instance failing is non-fatal
            continue
    return breakdown


@router.get("/capacity.xlsx")
def export_capacity_xlsx(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    group: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    dedup_zabbix: bool = Query(default=False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Export the filtered Capacity view as a branded .xlsx (same SQL filters).

    The date range is period metadata for the sheet header only — it must NOT
    filter hosts by last_seen, or an export window ending before the latest
    collection produces an empty sheet (capacity is a current snapshot).

    Dynatrace extras: a per-partition disk column (live from the Metrics API),
    and ``dedup_zabbix=true`` drops hosts already monitored by Zabbix (matched
    by IP or hostname) since Zabbix is the primary tool.
    """
    _require_export(settings)
    from app.export_xlsx import build_workbook
    from app.routers.pages import _hosts_stmt

    stmt = _hosts_stmt(q, platform, status, instance, group)
    stmt = stmt.order_by(Host.hostname.asc())

    is_dynatrace = platform == "dynatrace"
    disks_by_host = _live_disk_breakdown(settings) if is_dynatrace else {}
    zbx_ips, zbx_names = (
        _zabbix_identity(db) if (is_dynatrace and dedup_zabbix) else (set(), set())
    )

    def rows():
        for h in db.scalars(stmt):
            if is_dynatrace and dedup_zabbix and _in_zabbix(h, zbx_ips, zbx_names):
                continue
            m = h.metrics or {}
            row = [
                h.hostname, h.ip or "", h.source_platform.value, h.source_instance,
                h.group_name or "",
                "" if h.cpu_pct is None else h.cpu_pct,
                "" if h.mem_pct is None else h.mem_pct,
                "" if h.disk_pct is None else h.disk_pct,
                m.get("cores", ""), m.get("mem_total_gb", ""),
                h.last_seen, h.status.value,
            ]
            if is_dynatrace:
                disks = disks_by_host.get(h.external_id) or []
                row.append(
                    " | ".join(
                        f"{label}: {used} / {total} GB" for label, total, used in disks
                    )
                )
            yield row

    columns = ["Server", "IP", "Platform", "Instance", "Group", "CPU %",
               "Memory %", "Disk %", "Cores", "Total Memory (GB)",
               "Last Updated", "Status"]
    if is_dynatrace:
        columns.append("Disks (used / total GB)")

    filters = ", ".join(
        f"{k}={v}" for k, v in (
            ("platform", platform), ("instance", instance), ("service", group),
            ("status", status), ("q", q),
            ("exclude Zabbix dups", "yes" if dedup_zabbix else ""),
        ) if v and v != "all"
    ) or "none"
    data = build_workbook(
        sheet_title="Capacity",
        period=_period_str(date_from, date_to),
        filters_summary=filters,
        columns=columns,
        rows=rows(),
    )
    fname = f"SAMIX_capacity_{_fname_stamp(date_from)}_{_fname_stamp(date_to)}.xlsx"
    return _xlsx_response(fname, data)


@router.get("/agents.xlsx")
def export_agents_xlsx(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    group: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Export the filtered Agents view as a branded, severity-colored .xlsx.

    Same columns and filters as the on-screen table — the whole matching set,
    not just the current page. The Alerts column carries each host's active
    alert count and is tinted by its highest severity, same convention as the
    Alerts export's own severity column.
    """
    _require_export(settings)
    from app.export_xlsx import build_workbook
    from app.routers.pages import _annotate_agent_alerts, _hosts_stmt

    stmt = _hosts_stmt(q, platform, status, instance, group)
    stmt = stmt.order_by(Host.hostname.asc())

    # Alert counts are a batched second query (see _annotate_agent_alerts),
    # not a per-row lookup — same query shape the live page already uses,
    # just run once over every matching host instead of one page of them.
    hosts = list(db.scalars(stmt))
    _annotate_agent_alerts(db, hosts)

    def rows():
        for h in hosts:
            yield [
                h.hostname, h.ip or "", h.source_platform.value,
                h.source_instance or "", h.group_name or "", h.status.value,
                h.last_seen, h.alert_count,          # type: ignore[attr-defined]
                h.max_sev,                           # type: ignore[attr-defined]
            ]

    filters = ", ".join(
        f"{k}={v}" for k, v in (
            ("platform", platform), ("instance", instance), ("service", group),
            ("status", status), ("q", q),
        ) if v and v != "all"
    ) or "none"
    data = build_workbook(
        sheet_title="Agents",
        period="current snapshot",
        filters_summary=filters,
        columns=["Agent", "IP", "Platform", "Instance", "Service / Group",
                 "Status", "Last Updated", "Alerts"],
        rows=rows(),
        severity_col=7,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    fname = f"SAMIX_agents_{stamp}.xlsx"
    return _xlsx_response(fname, data)


@router.get("/alerts.xlsx")
def export_alerts_xlsx(
    state: str = Query(default="active"),
    active: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    group: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Export the filtered Alerts view as a branded, severity-colored .xlsx.

    ``state`` is active/resolved/all. ``active`` is kept for backward compat:
    ``active=false`` maps to ``all`` when ``state`` isn't given explicitly.
    ``group`` is the service / group filter, matched through the alert's host.
    """
    _require_export(settings)
    from app.export_xlsx import build_workbook
    from app.routers.pages import _alerts_filtered_stmt, parse_dt

    if active is False and state == "active":
        state = "all"
    if state not in ("active", "resolved", "all"):
        state = "active"

    stmt = _alerts_filtered_stmt(
        q, parse_dt(date_from), parse_dt(date_to), state, group
    )
    stmt = stmt.order_by(
        Alert.severity_int.desc(), Alert.started_at.desc().nullslast()
    )

    def rows():
        for a in db.scalars(stmt):
            # trailing severity_int drives the severity-cell color (not a column)
            yield [
                a.severity_label, a.title, a.source_platform.value,
                a.source_instance or "", a.host_hostname or "", a.started_at,
                "resolved" if a.resolved else "active", a.severity_int,
            ]

    filters = ", ".join(
        f"{k}={v}"
        for k, v in (("state", state), ("service", group), ("q", q))
        if v and v != "all"
    ) or "none"
    data = build_workbook(
        sheet_title="Alerts",
        period=_period_str(date_from, date_to),
        filters_summary=filters,
        columns=["Severity", "Title", "Platform", "Instance", "Host",
                 "Started", "State"],
        rows=rows(),
        severity_col=0,
    )
    fname = f"SAMIX_alerts_{_fname_stamp(date_from)}_{_fname_stamp(date_to)}.xlsx"
    return _xlsx_response(fname, data)


def _db_report_rows(
    db: Session,
    platform: str,
    instance: str | None = None,
    exclude_hostnames: set[str] | None = None,
) -> list[list]:
    """Best-effort weekly-report rows from stored host records (one per host).

    Used for Dynatrace (and as a fallback) where per-filesystem/trend data isn't
    pulled live: fills the same columns from the Host snapshot, N/A where unknown.

    ``exclude_hostnames`` (lowercased) drops hosts already covered by a live
    report. The exclusion runs in SQL rather than in the caller, so those rows
    are never loaded just to be discarded.
    """
    from app.zabbix_report import REPORT_COLUMNS  # noqa: F401 (column order)

    stmt = (
        select(Host)
        .options(defer(Host.raw_payload))
        .where(Host.source_platform == platform)
    )
    if instance and instance != "all":
        stmt = stmt.where(Host.source_instance == instance)
    if exclude_hostnames:
        names = sorted(exclude_hostnames)
        for i in range(0, len(names), 500):  # respect SQLite's bound-param limit
            stmt = stmt.where(func.lower(Host.hostname).notin_(names[i : i + 500]))
    stmt = stmt.order_by(Host.hostname.asc())
    rows: list[list] = []
    for h in db.scalars(stmt):
        m = h.metrics or {}
        rows.append([
            h.hostname, h.ip or "N/A",
            m.get("cores", 0), h.cpu_pct if h.cpu_pct is not None else 0,
            m.get("mem_total_gb", 0.0), int(h.mem_pct) if h.mem_pct is not None else 0,
            m.get("disk_total_gb", 0.0), m.get("disk_used_gb", 0.0),
            "N/A", "N/A",
            h.group_name or "N/A", "N/A",
            "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
        ])
    return rows


def _dynatrace_report_rows(
    db: Session, settings: Settings, dedup_zabbix: bool = False
) -> list[list]:
    """Weekly-report rows for Dynatrace hosts, one row per partition.

    Host-level fields come from the stored snapshot; per-partition disk usage
    (C:, D:, /…) is pulled live from any configured Dynatrace collector so the
    "VM Space / VM Used Space For Each Drive" columns match the Zabbix report.
    Best-effort: if the live breakdown is unavailable, each host gets a single
    row with the summed disk total. ``dedup_zabbix`` drops hosts already
    monitored by Zabbix (matched by IP or hostname).
    """
    breakdown = _live_disk_breakdown(settings)
    zbx_ips, zbx_names = _zabbix_identity(db) if dedup_zabbix else (set(), set())

    rows: list[list] = []
    stmt = (
        select(Host)
        .where(Host.source_platform == "dynatrace")
        .order_by(Host.hostname.asc())
    )
    for h in db.scalars(stmt):
        if dedup_zabbix and _in_zabbix(h, zbx_ips, zbx_names):
            continue
        m = h.metrics or {}
        base = [
            h.hostname, h.ip or "N/A",
            m.get("cores", 0), h.cpu_pct if h.cpu_pct is not None else 0,
            m.get("mem_total_gb", 0.0), int(h.mem_pct) if h.mem_pct is not None else 0,
            m.get("disk_total_gb", 0.0), m.get("disk_used_gb", 0.0),
            "N/A", "N/A",                       # per-drive (cols 8, 9) — filled below
            h.group_name or "N/A", "N/A",
            "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
        ]
        disks = breakdown.get(h.external_id)
        if disks:
            for label, total_gb, used_gb in sorted(disks):
                r = list(base)
                r[8] = f"{label} : {total_gb}"
                r[9] = f"{label} : {used_gb}"
                rows.append(r)
        else:
            rows.append(list(base))
    return rows


@router.get("/capacity_report.xlsx")
def export_capacity_report_xlsx(
    platform: str | None = Query(default="all"),
    instance: str | None = Query(default="all"),
    days: int = Query(default=7),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    dedup_zabbix: bool = Query(default=False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Rich capacity export (the weekly-report columns).

    Zabbix rows are pulled LIVE per instance (cores, cpu/mem/disk, per-filesystem,
    and min/avg/max CPU/RAM trends over the window); Dynatrace is best-effort from
    the stored snapshot. Export only — the on-screen view is unchanged.
    """
    _require_export(settings)
    from app.export_xlsx import build_workbook
    from app.routers.pages import parse_dt
    from app.servers import load_servers
    from app.zabbix_report import REPORT_COLUMNS, weekly_report_rows

    df, dt = parse_dt(date_from), parse_dt(date_to)
    if df and dt and dt > df:
        days = max(1, (dt - df).days or 1)

    rows: list[list] = []

    # --- Zabbix: live report per selected instance ---
    zbx_instances: list[str] = []
    if not settings.mock_mode and platform in (None, "all", "zabbix"):
        if instance and instance != "all":
            zbx_instances = [instance]
        else:
            zbx_instances = [s.name for s in load_servers(settings) if s.platform == "zabbix"]
    for inst in zbx_instances:
        collector = get_service().get(inst)
        if collector is None or getattr(collector, "name", "") != "zabbix":
            live_rows: list[list] = []
        else:
            try:
                live_rows = weekly_report_rows(collector, days)
            except Exception:  # noqa: BLE001 — one instance failing is non-fatal
                live_rows = []
        rows.extend(live_rows)

        # ``unknown`` is an availability state, not proof that Zabbix has no
        # capacity data.  The live report can omit such a host when an API
        # query is incomplete; preserve a usable row from the last collected
        # Host snapshot instead of producing an empty Excel sheet.
        live_hostnames = {str(row[0]).casefold() for row in live_rows if row}
        rows.extend(_db_report_rows(db, "zabbix", inst, live_hostnames))

    # --- Dynatrace: snapshot host-level + live per-partition disk ---
    if platform in (None, "all", "dynatrace") and (instance in (None, "all") or platform == "dynatrace"):
        rows.extend(_dynatrace_report_rows(db, settings, dedup_zabbix))

    # Mock/demo (no live Zabbix): fall back to the snapshot for the whole view.
    if settings.mock_mode and not rows:
        for pf in ("zabbix", "dynatrace"):
            rows.extend(_db_report_rows(db, pf))

    period = _period_str(date_from, date_to) if (df or dt) else f"trend window: last {days} days"
    filters = ", ".join(
        f"{k}={v}" for k, v in (("platform", platform), ("instance", instance)) if v and v != "all"
    ) or "none"
    data = build_workbook(
        sheet_title="Capacity Report",
        period=period,
        filters_summary=filters,
        columns=REPORT_COLUMNS,
        rows=rows,
    )
    fname = f"SAMIX_capacity_report_{_fname_stamp(date_from)}_{_fname_stamp(date_to)}.xlsx"
    return _xlsx_response(fname, data)


# --- Topology ---------------------------------------------------------------

#: Map the UI "view" name to the platform that owns that kind of graph.
_VIEW_PLATFORM = {
    "network": SourcePlatform.nnmi,
    "service": SourcePlatform.dynatrace,
}


def _require_topology(settings: Settings) -> None:
    """Reject the request when the topology feature is disabled by config."""
    if not settings.enable_topology:
        raise HTTPException(status_code=404, detail="Topology is disabled")


@router.get("/topology/graph")
def topology_graph(
    view: str = Query(default="network"),
    instance: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return the topology graph as Cytoscape.js elements JSON.

    ``view=network`` returns the NNMi L2 map; ``view=service`` returns the
    Dynatrace service dependency map. Filter to one instance with ``instance``.
    """
    _require_topology(settings)
    platform = _VIEW_PLATFORM.get(view, SourcePlatform.nnmi)

    node_stmt = select(TopologyNode).where(TopologyNode.source_platform == platform)
    edge_stmt = select(TopologyEdge).where(TopologyEdge.source_platform == platform)
    if instance and instance != "all":
        node_stmt = node_stmt.where(TopologyNode.source_instance == instance)
        edge_stmt = edge_stmt.where(TopologyEdge.source_instance == instance)

    nodes = list(db.scalars(node_stmt).all())
    edges = list(db.scalars(edge_stmt).all())
    # A node is keyed per instance, so scope the id by instance to stay unique
    # even when two instances share an external_id.
    def nid(inst: str, ext: str) -> str:
        return f"{inst}::{ext}"

    known = {nid(n.source_instance, n.external_id) for n in nodes}
    el_nodes = [
        {
            "data": {
                "id": nid(n.source_instance, n.external_id),
                "label": n.name,
                "kind": n.kind,
                "category": n.category or "",
                "status": (n.status or "").upper(),
                "instance": n.source_instance,
            }
        }
        for n in nodes
    ]
    el_edges = []
    for e in edges:
        src = nid(e.source_instance, e.from_external_id)
        dst = nid(e.source_instance, e.to_external_id)
        if src not in known or dst not in known:
            continue
        el_edges.append(
            {
                "data": {
                    "id": nid(e.source_instance, e.external_id),
                    "source": src,
                    "target": dst,
                    "label": e.label or "",
                    "kind": e.kind,
                }
            }
        )
    return {
        "view": view,
        "platform": platform.value,
        "instance": instance or "all",
        "counts": {"nodes": len(el_nodes), "edges": len(el_edges)},
        "elements": {"nodes": el_nodes, "edges": el_edges},
    }


@router.post("/forecast/run")
def forecast_run() -> dict[str, str]:
    """Refit every capacity series now, instead of waiting for 03:30.

    Fitting the whole estate is seconds of numpy, but it also prunes old
    samples and rewrites a table, so it goes to the background scheduler like
    every other manual trigger rather than holding the request open.
    """
    if request_forecast_run():
        return {"status": "queued"}
    run_forecast_now()
    return {"status": "ok"}


@router.get("/forecast")
def forecast_json(
    classification: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    group: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    """The current capacity forecast as JSON, same filters as the page."""
    from app.routers.pages import _forecast_rows

    rows, *_ = _forecast_rows(
        db, q, platform or "all", instance or "all", group or "all",
        classification or "at_risk", kind or "all", 1,
    )
    cap = min(limit or settings.api_default_limit, settings.api_max_limit)
    return [
        {
            "hostname": r["host"].hostname,
            "ip": r["host"].ip,
            "platform": r["host"].source_platform.value,
            "instance": r["host"].source_instance,
            "service": r["host"].group_name,
            "metric_kind": r["f"].metric_kind,
            "subject": r["f"].subject or None,
            "current_pct": r["f"].current_pct,
            "slope_pct_per_day": r["f"].slope_pct_per_day,
            "r_squared": r["f"].r_squared,
            "days_to_threshold_90": r["f"].days_to_threshold_90,
            "days_to_full": r["f"].days_to_full,
            "classification": r["f"].classification,
            "reason": r["f"].reason,
            "total_gb": r["f"].total_value,
            "sample_count": r["f"].sample_count,
            "computed_at": r["f"].computed_at,
        }
        for r in rows[:cap]
    ]


_FORECAST_COLUMNS = [
    "Server", "IP", "Platform", "Instance", "Service", "Resource", "Drive",
    "Size (GB)", "Current %", "Trend (%/day)", "Days to 90%", "Days to full",
    "Confidence (R²)", "Outlook", "Daily points", "Note",
]


def _confidence_label(r_squared: float | None) -> str:
    """R² as the same three words the table shows."""
    if r_squared is None:
        return ""
    if r_squared >= 0.7:
        return "High"
    return "Med" if r_squared >= 0.3 else "Low"


@router.get("/forecast.xlsx")
def forecast_xlsx(
    classification: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    group: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Export the filtered forecast as a branded .xlsx.

    The date range is period metadata for the sheet header only, exactly as on
    the Capacity export: the forecast is the latest computed result, not a
    query over a window.
    """
    _require_export(settings)
    from app.export_xlsx import build_workbook
    from app.routers.pages import _forecast_rows

    rows, *_ = _forecast_rows(
        db, q, platform or "all", instance or "all", group or "all",
        classification or "at_risk", kind or "all", 1,
    )

    #: Tint the Outlook cell with the alert palette, so a printed sheet reads
    #: the same as the screen. build_workbook takes the severity as a trailing
    #: value it pops off each row.
    severity_of = {
        "critical": 5, "warning": 4, "watch": 3, "noisy": 2,
        "ok": 1, "insufficient_data": 1,
    }

    def data():
        for r in rows:
            f, host = r["f"], r["host"]
            yield [
                host.hostname, host.ip or "", host.source_platform.value,
                host.source_instance, host.group_name or "",
                f.metric_kind, f.subject or "",
                "" if f.total_value is None else round(f.total_value, 1),
                "" if f.current_pct is None else f.current_pct,
                "" if f.slope_pct_per_day is None else f.slope_pct_per_day,
                "" if f.days_to_threshold_90 is None else round(f.days_to_threshold_90),
                "" if f.days_to_full is None else round(f.days_to_full),
                "" if f.r_squared is None else f.r_squared,
                f.classification,
                f.sample_count,
                f.reason or "",
                severity_of.get(f.classification, 1),
            ]

    filters = ", ".join(
        f"{k}={v}" for k, v in (
            ("classification", classification), ("resource", kind),
            ("platform", platform), ("instance", instance),
            ("service", group), ("q", q),
        ) if v and v != "all"
    ) or "none"

    payload = build_workbook(
        sheet_title="Capacity forecast",
        period=_period_str(date_from, date_to),
        filters_summary=filters,
        columns=_FORECAST_COLUMNS,
        rows=data(),
        severity_col=_FORECAST_COLUMNS.index("Outlook"),
        credit=(
            f"Linear trend over the last {settings.forecast_window_days} days. "
            f"Forecasts with R² below {settings.forecast_min_r_squared:g} are "
            "reported without dates."
        ),
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="capacity-forecast-{stamp}.xlsx"'
        },
    )


@router.post("/topology/run")
def topology_run(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Queue a rebuild of every instance's topology graph.

    A rebuild walks NNMi's SOAP API and Dynatrace's entity API in full, so it
    goes to the background scheduler rather than holding the request open.
    """
    _require_topology(settings)
    if request_topology_run():
        return {"status": "queued"}
    run_topology_now()
    return {"status": "ok"}


def _require_topology_export(settings: Settings) -> None:
    """Reject the request when topology export is disabled by config."""
    if not settings.enable_topology or not settings.enable_topology_export:
        raise HTTPException(status_code=404, detail="Topology export is disabled")


def _topology_edges(
    db: Session, platform: SourcePlatform, instance: str | None
) -> list[TopologyEdge]:
    stmt = select(TopologyEdge).where(TopologyEdge.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(TopologyEdge.source_instance == instance)
    return list(db.scalars(stmt).all())


@router.get("/topology/nnmi-l2.csv")
def export_nnmi_l2_csv(
    instance: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Export the NNMi L2 connections (name, status, endpoints, interfaces)."""
    _require_topology_export(settings)
    edges = _topology_edges(db, SourcePlatform.nnmi, instance)
    rows = nnmi_connection_rows(edges)
    return _csv_response(
        "nnmi_l2_connections.csv",
        NNMI_L2_COLUMNS,
        [[r[c] for c in NNMI_L2_COLUMNS] for r in rows],
    )


@router.get("/topology/dynatrace-map.xlsx")
def export_dynatrace_map_xlsx(
    instance: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Export the Dynatrace unified map as an XLSX with three sheets.

    Sheets: ``Unified_Map`` (per-path), ``App_to_App`` and ``Service_to_Service``
    — the same shape as the ``Dynatrace_Inventory_Map.xlsx`` export.
    """
    _require_topology_export(settings)
    from openpyxl import Workbook  # local import: only needed for export
    from openpyxl.styles import Font, PatternFill

    edges = _topology_edges(db, SourcePlatform.dynatrace, instance)
    unified = dynatrace_unified_rows(edges)
    app2app = dynatrace_app_rows(unified)
    svc2svc = dynatrace_service_rows(unified)

    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(bold=True, color="FFFFFF")

    def _sheet(wb, title, columns, rows):
        ws = wb.create_sheet(title)
        ws.append([label for _key, label in columns])
        for c in range(1, len(columns) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font = hdr_fill, hdr_font
        for r in rows:
            ws.append([r.get(key, "") for key, _label in columns])
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
        return ws

    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Unified_Map", UNIFIED_COLUMNS, unified)
    _sheet(
        wb,
        "App_to_App",
        [
            ("source_application", "Source Application"),
            ("target_application", "Target Application"),
            ("link_type", "Link Type"),
        ],
        app2app,
    )
    _sheet(
        wb,
        "Service_to_Service",
        [
            ("source_service", "Source Service"),
            ("target_service", "Target Service"),
            ("link_type", "Link Type"),
            ("middleware_chain", "Middleware Chain"),
        ],
        svc2svc,
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="dynatrace_unified_map.xlsx"'
        },
    )


@router.get("/summary", response_model=SummaryOut)
def summary(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SummaryOut:
    """Return aggregate KPIs for the overview page."""
    total_hosts = db.scalar(select(func.count(Host.id))) or 0
    hosts_down = (
        db.scalar(
            select(func.count(Host.id)).where(Host.status == HostStatus.down)
        )
        or 0
    )
    active_alerts = (
        db.scalar(select(func.count(Alert.id)).where(Alert.resolved.is_(False)))
        or 0
    )

    # Per-platform host totals and down counts (two small grouped queries).
    def _by_platform(rows: list[tuple]) -> dict[str, int]:
        return {getattr(p, "value", str(p)): int(c) for p, c in rows}

    total_by_platform = _by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id)).group_by(
                Host.source_platform
            )
        ).all()
    )
    down_by_platform = _by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id))
            .where(Host.status == HostStatus.down)
            .group_by(Host.source_platform)
        ).all()
    )
    per_platform = [
        PlatformHostCount(
            platform=plat,
            total=total_by_platform.get(plat, 0),
            down=down_by_platform.get(plat, 0),
        )
        for plat in PLATFORM_ORDER
    ]

    # Active alerts by severity.
    sev_rows = db.execute(
        select(Alert.severity_int, func.count(Alert.id))
        .where(Alert.resolved.is_(False))
        .group_by(Alert.severity_int)
    ).all()
    sev_counts = {int(s): int(c) for s, c in sev_rows}
    severity_buckets = [
        SeverityBucket(
            severity_int=level,
            label=severity_label(level),
            count=sev_counts.get(level, 0),
        )
        for level in (5, 4, 3, 2, 1)
    ]

    collectors = get_collector_statuses(db, settings)

    return SummaryOut(
        total_hosts=total_hosts,
        hosts_down=hosts_down,
        active_alerts=active_alerts,
        per_platform=per_platform,
        severity_buckets=severity_buckets,
        collectors=collectors,
    )


@router.get("/collectors/status", response_model=list[CollectorStatus])
def collectors_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[CollectorStatus]:
    """Return current health for every configured instance."""
    return get_collector_statuses(db, settings)


@router.post("/collectors/{instance}/run")
def run_collector(instance: str) -> dict[str, str]:
    """Queue a single instance's collection run.

    Handed to the background scheduler so the request returns at once; a
    collection run talks to a remote monitoring API and can take minutes.
    """
    service = get_service()
    if service.get(instance) is None:
        raise HTTPException(status_code=404, detail=f"unknown instance: {instance}")
    if request_run_one(instance):
        return {"status": "queued", "instance": instance}
    service.run_one(instance)  # no scheduler (tests / pre-startup): run inline
    return {"status": "ok", "instance": instance}


@router.post("/collectors/run")
def run_all_collectors() -> dict[str, object]:
    """Queue a run of every configured instance.

    This backs the "Refresh now" button on every page. It returns immediately
    and lets the scheduler coalesce repeat clicks, so a burst of them can no
    longer tie up a threadpool thread each for the length of a full poll.
    """
    service = get_service()
    if request_run_all():
        return {"status": "queued", "instances": list(service.collectors)}
    service.run_all()
    return {"status": "ok", "instances": list(service.collectors)}


@router.post("/collectors/{instance}/test-mail")
def test_mail(
    instance: str,
    to: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Ask a Zabbix instance to send a test email (verifies email alerting)."""
    service = get_service()
    collector = service.get(instance)
    if collector is None or not hasattr(collector, "send_test_mail"):
        raise HTTPException(
            status_code=404,
            detail=f"instance '{instance}' not found or does not support test mail",
        )
    recipient = to or settings.test_mail_to
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail="no recipient: pass ?to=... or set TEST_MAIL_TO in .env",
        )
    result = collector.send_test_mail(recipient)  # type: ignore[attr-defined]
    return {"instance": instance, "to": recipient, **result}

