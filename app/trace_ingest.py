"""Distributed trace ingest — Correlation Phase 5.

No current collector pulls request-level trace data (Dynatrace's Problems v2
API, the only Dynatrace feed this app polls, aggregates a whole problem, not
individual spans — see app.canonical_event's own module note). This is the
push path for it: a real deployment would point Dynatrace's Distributed
Tracing/RUM export, or any other APM's trace exporter, at
``POST /api/v1/traces`` the same way the SiteScope forwarder already pushes
into ``POST /api/v1/sitescope/ingest`` (app.sitescope_ingest) rather than
being polled.

Each span is one hop of one request: a service, optionally the API endpoint
it serves, and optionally a database/external-service call it made. Two
things happen per batch:

1. The application-architecture topology those spans imply — Application
   *provides* Service *provides* API (an impact chain: the parent's trouble
   affects the child, same direction as the existing "LoadBalancer provides
   ServiceA" example in RelationshipType's own docstring), Service/API
   *depends_on* Database and *uses* ExternalService (a dependency chain: the
   callee's trouble affects the caller), and API *part_of* BusinessTransaction
   *part_of* BusinessService (again an impact chain, the same
   "API part_of Customer360" example) — is recorded via the EXISTING Phase 3
   graph (app.dependency_graph.record_relationship). No new relationship
   types; Phase 5 reuses Phase 3's directional vocabulary because it already
   fits.
2. Only ERRORING spans (``error`` true, or an HTTP ``status_code`` >= 500)
   become canonical events, fingerprinted and deduplicated through the same
   Phase 2 pipeline as every other source. A healthy span exists only to
   declare topology and to prove an application's OTHER endpoints are fine
   (see app.application_health) — never as an Alert row; a request that
   succeeded is not a "resolved" occurrence of anything.

Recovery is explicit, not inferred: a later span for the SAME
(trace_id, span_id) submitted with ``"resolved": true`` marks that specific
occurrence resolved — the same "a poll says this row is gone/fixed now"
signal every other collector already gives, not a guess that one span
"fixes" another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup import record_occurrences_batch
from app.dependency_graph import record_relationship
from app.entity_resolution import resolve_named_entity
from app.fingerprint import compute_fingerprint, normalize_problem_type
from app.models import Alert, EntityType, RelationshipType, SourcePlatform, TopologySource

logger = logging.getLogger("trace_ingest")


@dataclass
class TraceIngestResult:
    """Outcome of ingesting one batch of spans."""

    received: int = 0
    inserted: int = 0
    updated: int = 0
    resolved: int = 0
    relationships_declared: int = 0


def _cache_entity(db: Session, cache: dict[str, int], entity_type: EntityType, name: str | None) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    if name not in cache:
        cache[name] = resolve_named_entity(db, entity_type=entity_type, name=name)
    return cache[name]


def _declare_edge(
    db: Session, *, relationship_type: RelationshipType, from_id: int | None, to_id: int | None,
    source_reference: str, evidence: str,
) -> bool:
    if from_id is None or to_id is None or from_id == to_id:
        return False
    record_relationship(
        db, source=TopologySource.dynatrace, relationship_type=relationship_type,
        from_entity_id=from_id, to_entity_id=to_id,
        source_reference=source_reference, evidence=evidence,
    )
    return True


def ingest_trace_spans(db: Session, *, instance: str, spans: list[dict]) -> TraceIngestResult:
    """Ingest one batch of trace spans. See module docstring for the shape
    each span dict is expected to carry (all keys but ``trace_id``/``span_id``
    are optional): ``parent_span_id``, ``application``, ``service``, ``api``
    (or ``endpoint``), ``http_method``, ``database``, ``external_service``,
    ``business_transaction``, ``business_service``, ``duration_ms``,
    ``status_code``, ``error`` (bool), ``resolved`` (bool), ``started_at``.
    """
    result = TraceIngestResult(received=len(spans))
    if not spans:
        return result

    application_ids: dict[str, int] = {}
    service_ids: dict[str, int] = {}
    api_ids: dict[str, int] = {}
    database_ids: dict[str, int] = {}
    external_ids: dict[str, int] = {}
    bt_ids: dict[str, int] = {}
    bs_ids: dict[str, int] = {}

    edges_declared = 0
    for span in spans:
        api_name = span.get("api") or span.get("endpoint")
        app_id = _cache_entity(db, application_ids, EntityType.application, span.get("application"))
        svc_id = _cache_entity(db, service_ids, EntityType.service, span.get("service"))
        api_id = _cache_entity(db, api_ids, EntityType.api, api_name)
        db_id = _cache_entity(db, database_ids, EntityType.database, span.get("database"))
        ext_id = _cache_entity(db, external_ids, EntityType.external_service, span.get("external_service"))
        bt_id = _cache_entity(db, bt_ids, EntityType.business_transaction, span.get("business_transaction"))
        bs_id = _cache_entity(db, bs_ids, EntityType.business_service, span.get("business_service"))

        ref = f"trace:{span['trace_id']}:{span['span_id']}"
        # Application -> Service -> API: an impact chain, parent's trouble
        # reaches the child (RelationshipType.provides — see module note).
        api_parent = svc_id if svc_id is not None else app_id
        for from_id, to_id, evidence in (
            (app_id, svc_id, f"trace: {span.get('application')} provides {span.get('service')}"),
            (api_parent, api_id, f"trace: {span.get('service') or span.get('application')} provides {api_name}"),
        ):
            if _declare_edge(
                db, relationship_type=RelationshipType.provides, from_id=from_id, to_id=to_id,
                source_reference=ref, evidence=evidence,
            ):
                edges_declared += 1

        # Service/API -> Database/ExternalService: a dependency chain, the
        # callee's trouble propagates back to the caller.
        dep_anchor = svc_id if svc_id is not None else api_id
        for rel_type, to_id, evidence in (
            (RelationshipType.depends_on, db_id, f"trace: depends on database {span.get('database')}"),
            (RelationshipType.uses, ext_id, f"trace: uses {span.get('external_service')}"),
        ):
            if _declare_edge(
                db, relationship_type=rel_type, from_id=dep_anchor, to_id=to_id,
                source_reference=ref, evidence=evidence,
            ):
                edges_declared += 1

        # API -> BusinessTransaction -> BusinessService: an impact chain in
        # the OTHER direction — the part's trouble is part of the whole's
        # (RelationshipType.part_of — the exact "API part_of Customer360"
        # example in that enum's own docstring).
        bt_or_bs = bt_id if bt_id is not None else bs_id
        for from_id, to_id, evidence in (
            (api_id, bt_or_bs, f"trace: {api_name} part_of {span.get('business_transaction') or span.get('business_service')}"),
            (bt_id, bs_id, f"trace: {span.get('business_transaction')} part_of {span.get('business_service')}"),
        ):
            if _declare_edge(
                db, relationship_type=RelationshipType.part_of, from_id=from_id, to_id=to_id,
                source_reference=ref, evidence=evidence,
            ):
                edges_declared += 1

    result.relationships_declared = edges_declared
    db.flush()

    external_id_of = {(s["trace_id"], s["span_id"]): f"{s['trace_id']}:{s['span_id']}" for s in spans}
    existing_rows: dict[str, Alert] = {
        row.external_id: row
        for row in db.scalars(
            select(Alert).where(
                Alert.source_platform == SourcePlatform.dynatrace,
                Alert.source_instance == instance,
                Alert.external_id.in_(set(external_id_of.values())),
            )
        ).all()
    }

    siblings_by_parent: dict[tuple[str, str], list[dict]] = {}
    for s in spans:
        if s.get("parent_span_id"):
            siblings_by_parent.setdefault((s["trace_id"], s["parent_span_id"]), []).append(s)

    now = datetime.now(timezone.utc)
    touched: list[Alert] = []

    # Explicit recoveries first — a later span for the same (trace_id,
    # span_id) marked resolved, independent of whether THIS batch also
    # contains errors for other spans.
    for span in spans:
        if not span.get("resolved"):
            continue
        row = existing_rows.get(external_id_of[(span["trace_id"], span["span_id"])])
        if row is not None and not row.resolved:
            row.resolved = True
            row.resolved_at = now
            row.status = "resolved"
            row.updated_at = now
            touched.append(row)
            result.resolved += 1

    error_spans = [
        s for s in spans
        if not s.get("resolved") and (s.get("error") or (s.get("status_code") and s["status_code"] >= 500))
    ]

    for span in error_spans:
        ext_id = external_id_of[(span["trace_id"], span["span_id"])]
        row = existing_rows.get(ext_id)
        is_new = row is None
        if row is None:
            row = Alert(
                source_platform=SourcePlatform.dynatrace, source_instance=instance, external_id=ext_id,
            )
            db.add(row)
            existing_rows[ext_id] = row
            result.inserted += 1
        else:
            result.updated += 1

        api_name = span.get("api") or span.get("endpoint")
        method = span.get("http_method")
        # Same "most specific" precedence as the entity_id assignment below —
        # a database/external-service span's title must name what's actually
        # failing (the DB/external call), not the ambient service context.
        subject = api_name or span.get("database") or span.get("external_service") or span.get("service") or "span"
        title = f"{method} {subject}".strip() if method else subject
        if span.get("status_code"):
            title = f"{title} HTTP {span['status_code']}"

        row.title = title
        row.severity_int = 4
        row.severity_label = "High"
        row.started_at = span.get("started_at") or now
        row.resolved = False
        row.status = "open"
        row.last_seen = now
        row.updated_at = now
        row.payload_ref = f"dynatrace:{instance}:{ext_id}"
        if is_new:
            row.original_severity = row.severity_label
            row.original_description = title

        row.event_type = "dynatrace_trace_span"
        row.problem_type = "HTTP_ERROR" if span.get("status_code") else "TRACE_ERROR"
        row.trace_id = span["trace_id"]
        row.span_id = span["span_id"]
        row.parent_span_id = span.get("parent_span_id")
        row.http_method = method
        row.endpoint = api_name
        row.http_status_code = span.get("status_code")
        row.duration_ms = span.get("duration_ms")
        row.is_error = True
        row.business_service = span.get("business_service")

        row.api_id = api_ids.get(api_name or "")
        row.api_name = api_name if row.api_id else None
        row.service_id = service_ids.get(span.get("service") or "")
        row.service_name = span.get("service") if row.service_id else None
        row.application_id = application_ids.get(span.get("application") or "")
        row.application_name = span.get("application") if row.application_id else None
        row.database_id = database_ids.get(span.get("database") or "")
        row.database_name = span.get("database") if row.database_id else None

        # entity_id/entity_type: the MOST SPECIFIC entity this span names —
        # what a Correlation Phase 4 same_api/same_database/... signal and a
        # Phase 5 root-cause candidate both key off.
        if row.api_id is not None:
            row.entity_id, row.entity_type = row.api_id, EntityType.api.value
        elif row.database_id is not None:
            row.entity_id, row.entity_type = row.database_id, EntityType.database.value
        elif row.service_id is not None:
            row.entity_id, row.entity_type = row.service_id, EntityType.service.value
        elif row.application_id is not None:
            row.entity_id, row.entity_type = row.application_id, EntityType.application.value
        else:
            row.entity_id, row.entity_type = None, None

        siblings = siblings_by_parent.get((span["trace_id"], span["span_id"]), [])
        row.db_calls = sorted({s["database"] for s in siblings if s.get("database")})
        row.external_calls = sorted({s["external_service"] for s in siblings if s.get("external_service")})

        row.normalized_problem_type = normalize_problem_type(problem_type=row.problem_type, title=row.title)
        row.fingerprint = compute_fingerprint(
            entity_id=row.entity_id, normalized_problem_type=row.normalized_problem_type,
            api_id=row.api_id, service_id=row.service_id, database_id=row.database_id,
        )
        touched.append(row)

    db.flush()
    record_occurrences_batch(db, touched)
    logger.info(
        "trace ingest (%s): %d span(s) received, %d inserted, %d updated, %d resolved, %d relationship(s) declared",
        instance, result.received, result.inserted, result.updated, result.resolved,
        result.relationships_declared,
    )
    return result
