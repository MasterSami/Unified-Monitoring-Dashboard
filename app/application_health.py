"""Application-level health rollup — Correlation Phase 5.

An Application is never "down" just because one of its APIs is failing.
Status is a deterministic function of how many of its KNOWN APIs currently
have an active LogicalEvent against them — never a guess, and never based on
the Application's own events alone when it has APIs to roll up from:

    GET /customer         healthy
    POST /customer/update failed
    DELETE /customer      healthy
    -> Application: DEGRADED, affected API: POST /customer/update

"Known APIs" comes from the SAME topology graph Phase 3 built and Phase 5's
app.trace_ingest populates (Application --provides--> Service
--provides--> API, an impact chain) — walked with the existing
app.dependency_graph.traverse(direction="downstream"), unmodified. No new
graph-walking logic: Phase 3's "what's affected if X fails" traversal IS the
question "what APIs does this Application have", since Application-level
trouble is already stored to propagate exactly that direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependency_graph import DEFAULT_MAX_DEPTH, traverse
from app.models import CanonicalEntity, EntityType, LogicalEvent, LogicalEventStatus

#: A LogicalEvent counts as "currently affecting" its entity in any state
#: other than resolved — same set app.correlation_engine treats as active.
ACTIVE_STATUSES = frozenset({
    LogicalEventStatus.open, LogicalEventStatus.updated,
    LogicalEventStatus.deduplicated, LogicalEventStatus.reopened,
})


@dataclass
class ApplicationHealth:
    application_entity_id: int
    application_name: str
    #: "healthy" | "degraded" | "down" — see compute_application_health.
    status: str
    total_apis: int
    affected_apis: list[dict] = field(default_factory=list)
    healthy_apis: list[dict] = field(default_factory=list)


def compute_application_health(
    db: Session, application_entity_id: int, *, max_depth: int = DEFAULT_MAX_DEPTH,
) -> ApplicationHealth | None:
    """None if ``application_entity_id`` doesn't exist or isn't an
    ``application`` entity; otherwise its current rollup status:

    - ``healthy``: no known API has an active event (or it has no known
      APIs and no active event of its own).
    - ``degraded``: some, but not all, of its known APIs are affected.
    - ``down``: every known API is affected — or, when it has no known APIs
      to roll up from at all, the Application entity's own event is active.
    """
    app_entity = db.get(CanonicalEntity, application_entity_id)
    if app_entity is None or app_entity.entity_type != EntityType.application:
        return None

    downstream = traverse(db, application_entity_id, direction="downstream", max_depth=max_depth)
    apis = {n.entity_id: n.canonical_name for n in downstream.nodes if n.entity_type == EntityType.api.value}

    if not apis:
        own_open = db.scalars(
            select(LogicalEvent.id).where(
                LogicalEvent.entity_id == application_entity_id,
                LogicalEvent.status.in_(ACTIVE_STATUSES),
            )
        ).all()
        status = "down" if own_open else "healthy"
        return ApplicationHealth(application_entity_id, app_entity.canonical_name, status, 0)

    open_events = db.scalars(
        select(LogicalEvent).where(
            LogicalEvent.entity_id.in_(apis), LogicalEvent.status.in_(ACTIVE_STATUSES),
        )
    ).all()
    events_by_api: dict[int, list[int]] = {}
    for ev in open_events:
        events_by_api.setdefault(ev.entity_id, []).append(ev.id)

    affected = [
        {"entity_id": eid, "canonical_name": name, "logical_event_ids": events_by_api[eid]}
        for eid, name in apis.items() if eid in events_by_api
    ]
    healthy = [
        {"entity_id": eid, "canonical_name": name}
        for eid, name in apis.items() if eid not in events_by_api
    ]

    if not affected:
        status = "healthy"
    elif len(affected) >= len(apis):
        status = "down"
    else:
        status = "degraded"

    return ApplicationHealth(
        application_entity_id, app_entity.canonical_name, status, len(apis), affected, healthy,
    )
