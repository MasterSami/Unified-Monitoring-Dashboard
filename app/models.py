"""SQLAlchemy ORM models for the unified monitoring data model.

Four monitoring tools (Zabbix, Dynatrace, NNMi, SiteScope) plus the Digital
View asset inventory are normalized into a shared
:class:`Host` / :class:`Alert` schema. :class:`CollectorRun` records the
outcome of each polling run for health tracking.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class SourcePlatform(str, enum.Enum):
    """Supported source platforms.

    The first four are monitoring tools that report live state.
    ``digitalview`` is different in kind: it is the Digital View (Huawei i2000)
    **asset inventory**, loaded from an exported workbook because the API port
    is closed to us. It tells us what exists and how big it is, never whether
    it is up — so its hosts carry ``unknown`` status by design.
    """

    zabbix = "zabbix"
    dynatrace = "dynatrace"
    nnmi = "nnmi"
    sitescope = "sitescope"
    digitalview = "digitalview"


#: Every platform, in the order the UI lists them. Monitoring tools first,
#: inventory sources last.
PLATFORM_ORDER: tuple[str, ...] = (
    "zabbix", "dynatrace", "nnmi", "sitescope", "digitalview",
)

#: Platforms that report live availability. An inventory source does not, so
#: counting its hosts as "up" would inflate every health figure on the site.
LIVE_PLATFORMS: frozenset[str] = frozenset(
    {"zabbix", "dynatrace", "nnmi", "sitescope"}
)


class HostStatus(str, enum.Enum):
    """Normalized host availability status."""

    up = "up"
    down = "down"
    unknown = "unknown"
    disabled = "disabled"


class EntityType(str, enum.Enum):
    """Kinds of thing a :class:`CanonicalEntity` can represent.

    Phase 1 (entity resolution) only ever created ``host`` entities — the
    only kind that phase's collectors could resolve deterministically.
    Phase 3 (app/topology_sync.py) additionally resolves ``service`` and
    ``network_device`` entities from Dynatrace/NNMi topology data, reusing
    the exact same resolver. The remaining kinds stay schema-ready for
    whatever a later phase or a manually-declared relationship needs.
    """

    host = "host"
    network_device = "network_device"
    application = "application"
    service = "service"
    api = "api"
    database = "database"
    external_service = "external_service"
    business_service = "business_service"
    load_balancer = "load_balancer"
    #: A named business flow (e.g. "Update Customer") that sits between a
    #: business_service and the API(s) it drives — Correlation Phase 5.
    business_transaction = "business_transaction"


class ResolutionMethod(str, enum.Enum):
    """How a source's identifier was resolved to a :class:`CanonicalEntity`.

    Ordered by the priority :func:`app.entity_resolution.resolve_host_entity`
    checks them in (strongest evidence first). ``new_entity`` is not really a
    "match" — nothing matched, so a new identity was established; it is
    marked with full confidence because it asserts nothing beyond "this is a
    thing we have not seen before". ``conflict`` means the input evidence
    pointed at more than one existing entity — resolution deliberately
    refuses to guess.
    """

    manual_mapping = "manual_mapping"
    cmdb_exact_match = "cmdb_exact_match"
    ip_exact_match = "ip_exact_match"
    fqdn_exact_match = "fqdn_exact_match"
    hostname_exact_match = "hostname_exact_match"
    alias_match = "alias_match"
    source_mapping = "source_mapping"
    new_entity = "new_entity"
    conflict = "conflict"


class LogicalEventStatus(str, enum.Enum):
    """A :class:`LogicalEvent`'s lifecycle state (Correlation Phase 2).

    Recomputed from scratch every time the logical event is touched — never
    incrementally maintained — so the stored value can never drift from what
    a fresh scan of its occurrences would show. See
    ``app.dedup._recompute_logical_events`` for the exact, deterministic rule
    each state is derived from:

    - ``open``: exactly one occurrence, unresolved, unchanged since it was
      first seen.
    - ``updated``: still exactly one occurrence, but its severity has moved
      on from what it started at (``Alert.severity_label != original_severity``).
    - ``deduplicated``: two or more occurrences are folded into this logical
      event and at least one is still unresolved — the steady state for a
      recurring condition.
    - ``resolved``: every occurrence linked to it is resolved.
    - ``reopened``: it had reached ``resolved``, and a new, unresolved
      occurrence has since linked to it.
    """

    open = "open"
    updated = "updated"
    deduplicated = "deduplicated"
    resolved = "resolved"
    reopened = "reopened"


class RelationshipType(str, enum.Enum):
    """How one :class:`CanonicalEntity` relates to another (Phase 3).

    Split into two directional families for dependency-graph traversal —
    see app/dependency_graph.py:

    - "dependency" edges (``depends_on``, ``calls``, ``hosted_on``,
      ``connects_to``, ``routes_to``, ``uses``): stored as *from* needs
      *to*. A failure of ``to`` propagates back to ``from`` — e.g.
      ``CustomerService --depends_on--> CustomerDB``: DB trouble affects
      CustomerService, not the other way round.
    - "impact" edges (``affects``, ``provides``, ``part_of``,
      ``member_of``): already phrased in the failure-propagation direction —
      *from*'s failure propagates forward to ``to``. ``LoadBalancer
      --provides--> ServiceA`` means the load balancer failing affects
      ServiceA, exactly as stored. ``part_of``/``member_of`` belong here,
      not with the dependency edges, even though they read like one: "API
      --part_of--> Customer360" does not mean the API needs Customer360 to
      function — it means the API's own trouble is part of what can go
      wrong with Customer360, so it propagates the SAME direction as the
      edge is stored, from the part to the whole.
    """

    depends_on = "depends_on"
    calls = "calls"
    hosted_on = "hosted_on"
    connects_to = "connects_to"
    routes_to = "routes_to"
    uses = "uses"
    provides = "provides"
    part_of = "part_of"
    member_of = "member_of"
    affects = "affects"


class TopologySource(str, enum.Enum):
    """Where a :class:`EntityRelationship` row came from.

    Multiple sources may each independently record what amounts to the same
    real-world relationship — CMDB and Dynatrace can both know
    "CustomerService depends on CustomerDB" — and neither is silently
    merged into or overwritten by the other; each keeps its own row, its
    own evidence, and its own confidence. See app/topology_sync.py for
    which sources this codebase can actually populate automatically today
    (``dynatrace``, ``monitoring``) versus which are only ever written by a
    human or a script calling the API directly (``explicit_config``,
    ``cmdb``, ``manual`` — this app has no live CMDB feed to connect to).
    """

    explicit_config = "explicit_config"
    cmdb = "cmdb"
    dynatrace = "dynatrace"
    monitoring = "monitoring"
    manual = "manual"


class CorrelationSignal(str, enum.Enum):
    """One deterministic check between two LogicalEvents (Correlation Phase 4).

    See app/correlation_engine.py for how each is actually computed. Three of
    these — ``same_host``, ``same_application``, ``multi_source`` — plus
    ``temporal_relationship`` are never, on their own or in combination with
    only each other, sufficient to correlate two events (the "false
    correlation protection" rule); at least one of the remaining signals must
    also be present. See ``WEAK_SIGNALS`` in that module.
    """

    same_entity = "same_entity"
    same_host = "same_host"
    same_service = "same_service"
    same_application = "same_application"
    same_api = "same_api"
    same_database = "same_database"
    same_trace = "same_trace"
    known_dependency = "known_dependency"
    temporal_relationship = "temporal_relationship"
    same_business_transaction = "same_business_transaction"
    multi_source = "multi_source"


class CorrelationType(str, enum.Enum):
    """What KIND of evidence a :class:`Correlation` is primarily built on —
    derived from its highest-priority contributing signal, with a NETWORK
    override when a network_device entity is involved in a dependency/
    topology match. See app.correlation_engine.correlation_type_for.
    """

    entity = "entity"
    temporal = "temporal"
    topology = "topology"
    dependency = "dependency"
    multi_source = "multi_source"
    service = "service"
    application = "application"
    api = "api"
    database = "database"
    network = "network"
    trace = "trace"
    business_transaction = "business_transaction"


class CorrelationStatus(str, enum.Enum):
    """A :class:`Correlation` group's lifecycle state, recomputed from its
    current membership each time it's touched — same "pure function of
    current data" philosophy as LogicalEventStatus. See
    app.correlation_engine._compute_correlation_status for the exact rule
    behind each one.

    - ``new``: just created this run.
    - ``updated``: existing group, membership grew this run.
    - ``correlated``: existing group, unchanged this run, 2+ active members.
    - ``split``: membership fell below 2 — no longer a valid pairing.
    - ``resolved``: every current member is resolved.
    - ``reopened``: was resolved, a new/active member has since joined.
    """

    new = "new"
    updated = "updated"
    correlated = "correlated"
    split = "split"
    resolved = "resolved"
    reopened = "reopened"


class RunStatus(str, enum.Enum):
    """Outcome of a collector run."""

    success = "success"
    failed = "failed"


class Host(Base):
    """A monitored host, normalized across all source platforms."""

    __tablename__ = "hosts"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "source_instance",
            "external_id",
            name="uq_host_platform_instance_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    #: Primary IP — the one shown in the table. For a multi-homed host this is
    #: the first address the source platform reports; it is not necessarily
    #: "the" address anyone expects, which is what ip_all is for.
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Every IP the source platform knows for this host, comma-joined (same
    #: convention as group_name's comma-joined Zabbix groups). A host behind
    #: several NICs — common on Dynatrace, which reports a list — otherwise
    #: only ever matches a search on whichever address happened to be first;
    #: searching this column instead means a search for any of its addresses
    #: finds the host. ``None`` where a platform reports at most one IP.
    ip_all: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(
        Enum(SourcePlatform, native_enum=False, length=16), index=True
    )
    #: Name of the specific instance this came from (e.g. "Zabbix-34").
    source_instance: Mapped[str] = mapped_column(String(64), default="", index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[HostStatus] = mapped_column(
        Enum(HostStatus, native_enum=False, length=16),
        default=HostStatus.unknown,
        index=True,
    )
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Responsible person/team for this server, and their email (for Escalate).
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Is a monitoring agent actually deployed on this host?
    #:
    #: Dynatrace's entity API returns every host it has *discovered*, including
    #: ones seen only as a network peer with no OneAgent on them. Counting those
    #: as monitored inflates the estate — the number an operator recognises is
    #: the one on Dynatrace's own OneAgent deployment page. ``None`` means the
    #: platform does not draw this distinction (Zabbix, NNMi).
    agent_deployed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- Capacity metrics (utilization %, for the Capacity view) ------------
    #: CPU / memory / disk utilization as a percentage (0..100), when known.
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Extra capacity attributes (cores, mem_total_gb, disk_total_gb, …).
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # --- Entity resolution (Correlation Phase 1) -----------------------------
    #: Fully-qualified domain name, when the source reports one distinctly
    #: from ``hostname``. Used for FQDN_EXACT_MATCH — one rung below IP,
    #: one above hostname, since an FQDN collides across environments far
    #: less often than a bare hostname does.
    fqdn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: An external CMDB/asset identifier, when the source has one (e.g. the
    #: Digital View asset serial). Used for CMDB_EXACT_MATCH.
    cmdb_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: The :class:`CanonicalEntity` this source row was resolved to, and how.
    #: Plain int/string columns, not a FK — same convention as every other
    #: cross-entity reference in this schema (see module docstring).
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resolution_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class Alert(Base):
    """A canonical monitoring event, normalized across all platforms.

    This is the canonical event model for Correlation Phase 1: every field a
    source can report (severity, host identity, timestamps) plus the columns
    a later correlation phase needs (resolved entity, metric context, trace
    context) live on this one row, rather than in a second "Event" table —
    ``Alert`` already *is* "one normalized monitoring event", so a parallel
    table would just be the same rows twice under a different name.

    The original values a source reported are never overwritten once set:
    ``original_severity``/``original_description`` are set only on first
    insert (see :func:`app.normalizer.upsert_alerts`), even though
    ``severity_label``/``title`` continue to track the source's current view
    on every later poll. ``raw_payload`` keeps the entire original payload
    regardless.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "source_instance",
            "external_id",
            name="uq_alert_platform_instance_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(
        Enum(SourcePlatform, native_enum=False, length=16), index=True
    )
    #: Name of the specific instance this came from (e.g. "Zabbix-34").
    source_instance: Mapped[str] = mapped_column(String(64), default="", index=True)
    host_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: External id of the underlying HOST (Zabbix hostid / Dynatrace HOST-…),
    #: used to resolve the server owner for Escalate even when host_hostname is
    #: a service/entity name that doesn't match a Host row.
    host_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    severity_int: Mapped[int] = mapped_column(Integer, default=1, index=True)
    severity_label: Mapped[str] = mapped_column(String(32), default="info")
    title: Mapped[str] = mapped_column(String(512), default="")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: When ``resolved`` last flipped True — set once, at that transition
    #: (never on every poll like ``updated_at``), so a Correlation Phase 5
    #: incident timeline can show a real recovery time distinct from "this
    #: row was merely touched again". Null until the first resolution.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- SiteScope context (nullable; other platforms leave these unset) ----
    #: Raw event state (e.g. "back to default", "error"). State-wins drives sev.
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Correlation key: hostname lowercased, domain stripped (shared convention).
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    #: True for events carrying "Alert has no defined Metric" (kept, flagged).
    metric_missing: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Full monitor path (group hierarchy + monitor name).
    monitor_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # --- Canonical event: identity & lifecycle -------------------------------
    #: "open" | "resolved" — a platform-independent mirror of ``resolved``,
    #: for callers that want a single status field regardless of source.
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: Last time this exact source event was seen again (every poll bumps
    #: this and ``updated_at`` together; kept as its own column because a
    #: later phase may need "still active as of" independent of row edits).
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The severity/description exactly as first reported, preserved even
    #: after ``severity_label``/``title`` move on to reflect the source's
    #: current view — set once, on insert, never overwritten.
    original_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: How this event's raw payload can be retrieved again. ``raw_payload``
    #: is stored inline on this same row today, so this is currently just a
    #: locator for it (``platform:instance:external_id``) — kept as its own
    #: field so payloads can move to external/cold storage later without a
    #: schema change to whatever already reads this column.
    payload_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Canonical event: resolved entity (copied from the matched Host at
    # write time by app.normalizer._apply_canonical_fields) ------------------
    host_ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fqdn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Named host_owner, not owner: app.routers.pages._attach_escalation
    #: already sets a *transient* (never persisted) ``.owner`` attribute on
    #: Alert objects for the Escalate feature's read-time owner lookup — a
    #: same-named mapped column here would shadow it and risk the transient
    #: value being written back on a session that happens to flush/commit.
    host_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Matches CanonicalEntity.id (see app/entity_resolution.py), not a FK.
    #: Always entity_type="host" in Phase 1 — only Host rows are resolved.
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Canonical event: source-specific adapter output ---------------------
    #: A fixed label per platform (e.g. "zabbix_trigger"), set by the
    #: matching function in app/canonical_event.py.
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The closest thing the source exposes to a problem category, where one
    #: exists (e.g. Dynatrace's rankedEvents[0].eventType). Null where the
    #: source has nothing beyond severity + a free-text title.
    problem_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Tags/labels carried on the source payload, passed through as-is.
    tags: Mapped[list] = mapped_column(JSON, default=list)

    # --- Canonical event: broader context (schema-ready, mostly unpopulated
    # in Phase 1 — no current collector resolves these; see module note in
    # app/canonical_event.py). Kept here rather than in a later migration so
    # a Phase 2+ correlation engine has a stable column to write into. -------
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    application_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    http_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    database_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    network_device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    business_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Distributed trace context (Correlation Phase 5) — populated only by
    #: app.trace_ingest, the one path that ingests per-request span data;
    #: every other collector leaves these null, same as the fields above.
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Explicit error flag, independent of severity_int — a span carries no
    #: Zabbix-style severity of its own; this is what a trace-derived
    #: problem actually failed on.
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Names of databases/external services this span's sibling spans (same
    #: trace_id, parent_span_id == this span's span_id) called — read
    #: straight off the trace, never guessed.
    db_calls: Mapped[list] = mapped_column(JSON, default=list)
    external_calls: Mapped[list] = mapped_column(JSON, default=list)

    # --- Deduplication (Correlation Phase 2) ---------------------------------
    #: This platform's problem_type/title/metric_name reduced to a stable
    #: category with no dynamic values ("CPU utilization is 97%" -> "CPU_HIGH")
    #: — see app/fingerprint.py. Never used for entity resolution.
    normalized_problem_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Deterministic dedup key: entity + normalized_problem_type (+ metric/
    #: api/service/database/network_device where relevant). Kept as the raw
    #: composite string, not a hash, so it stays directly readable — see
    #: app/fingerprint.py:compute_fingerprint. Null when the alert has no
    #: resolved entity (see the same module) — nothing to fingerprint against.
    fingerprint: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    #: Matches LogicalEvent.id (see app/dedup.py), not a FK — same convention
    #: as entity_id.
    logical_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class TopologyNode(Base):
    """A node in a topology graph (NNMi network device or Dynatrace service)."""

    __tablename__ = "topology_nodes"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "source_instance",
            "external_id",
            name="uq_topo_node_platform_instance_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(
        Enum(SourcePlatform, native_enum=False, length=16), index=True
    )
    source_instance: Mapped[str] = mapped_column(String(64), default="", index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    #: device (NNMi) | service | middleware | application (Dynatrace)
    kind: Mapped[str] = mapped_column(String(24), default="node", index=True)
    name: Mapped[str] = mapped_column(String(512), default="")
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TopologyEdge(Base):
    """A link between two topology nodes (NNMi L2 link or Dynatrace call)."""

    __tablename__ = "topology_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "source_instance",
            "external_id",
            name="uq_topo_edge_platform_instance_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(
        Enum(SourcePlatform, native_enum=False, length=16), index=True
    )
    source_instance: Mapped[str] = mapped_column(String(64), default="", index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    from_external_id: Mapped[str] = mapped_column(String(255), index=True)
    to_external_id: Mapped[str] = mapped_column(String(255), index=True)
    #: l2 (NNMi) | call | via-middleware (Dynatrace)
    kind: Mapped[str] = mapped_column(String(24), default="link")
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CapacityHistory(Base):
    """One capacity sample for one series, appended over time.

    A *series* is ``(host, metric_kind, subject)`` — e.g. disk ``/var/lib`` on
    host 41, or that host's memory. :class:`Host` only ever holds the latest
    reading (the collectors overwrite ``cpu_pct`` / ``mem_pct`` / ``disk_pct``
    on every poll), so nothing in the schema could answer "is this filling up?"
    until this table existed. Forecasting reads only from here.

    ``subject`` is ``""`` (not NULL) for host-level metrics — memory and CPU —
    so the uniqueness constraint actually holds. SQL never treats one NULL as
    equal to another, on either SQLite or PostgreSQL, so a nullable subject in
    the unique index would silently admit duplicate daily rows and quietly
    double-weight those days in the regression.
    """

    __tablename__ = "capacity_history"
    __table_args__ = (
        UniqueConstraint(
            "host_id",
            "metric_kind",
            "subject",
            "sampled_at",
            name="uq_caphist_series_sample",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="", index=True)
    #: disk | memory | cpu
    metric_kind: Mapped[str] = mapped_column(String(16), index=True)
    #: Drive / mount point for disk series ("C:", "/var/lib"); "" otherwise.
    subject: Mapped[str] = mapped_column(String(255), default="")
    #: Absolute used / total in the series' natural unit (GB for disk and
    #: memory, cores for CPU). Both may be NULL when a template reports only a
    #: percentage — ``used_pct`` is the column the forecast actually fits.
    used_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_pct: Mapped[float] = mapped_column(Float)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class CapacityForecast(Base):
    """The current forecast for one series — replaced wholesale each run.

    Pages read only from this table. The regression runs in the nightly job so
    that opening /forecast is a single indexed SELECT, never 900 curve fits.
    """

    __tablename__ = "capacity_forecast"
    __table_args__ = (
        UniqueConstraint(
            "host_id", "metric_kind", "subject", name="uq_capfc_series"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="", index=True)
    metric_kind: Mapped[str] = mapped_column(String(16), index=True)
    subject: Mapped[str] = mapped_column(String(255), default="")

    current_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Percentage points per day. Negative means the series is draining.
    slope_pct_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_squared: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_to_threshold_90: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_to_full: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: critical | warning | watch | ok | noisy | insufficient_data
    classification: Mapped[str] = mapped_column(String(24), default="ok", index=True)
    #: Why a series was skipped or suppressed, shown in the UI as-is.
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Daily points the fit used, for the inline sparkline: [[day, pct], ...].
    points: Mapped[list] = mapped_column(JSON, default=list)
    #: Series size at the time of the fit (GB), for context in the table.
    total_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class CollectorRun(Base):
    """A single execution of a collector, recorded for health tracking."""

    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    #: Name of the specific instance this run polled (e.g. "Zabbix-34").
    instance: Mapped[str] = mapped_column(String(64), default="", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=16), default=RunStatus.success
    )
    items_collected: Mapped[int] = mapped_column(Integer, default=0)
    hosts_collected: Mapped[int] = mapped_column(Integer, default=0)
    alerts_collected: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)


# --- Correlation Phase 1: canonical entities ---------------------------------
#
# The same physical/logical thing (a host, later a service or application) is
# seen independently by several platforms under several different identifiers
# — Zabbix's "APP01", Dynatrace's "app-prod-01", NNMi's "APP01_TE". These four
# tables are where that gets resolved to one shared identity, deterministically
# and explainably (see app/entity_resolution.py for the matching algorithm).
#
# Kept as their own tables rather than folded into Host: a Host row already
# means "one platform's view of one thing" (that is the whole reason it is
# scoped to (platform, instance, external_id)); a CanonicalEntity means "the
# thing itself", which can outlive, predate, or be claimed by zero Host rows
# (e.g. a manual mapping declared before the source ever reports it, or an
# Application/Service entity no current collector produces a Host row for at
# all). Host gains only a plain entity_id/resolution_method/confidence — its
# own resolved answer — not a duplicate of this table's contents.


class CanonicalEntity(Base):
    """One resolved identity — a host, and in later phases a service, an
    application, etc. — shared across however many platforms report it.
    """

    __tablename__ = "canonical_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, native_enum=False, length=24),
        default=EntityType.host,
        index=True,
    )
    #: Best display name at creation time (usually the first source's
    #: hostname). Not re-derived later — renaming on every poll would make
    #: an entity's identity feel unstable even though its id never changes.
    canonical_name: Mapped[str] = mapped_column(String(255), default="")
    #: External CMDB/asset id, when one was available at resolution time.
    cmdb_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: Free-form extra context a later phase may want (e.g. a business unit
    #: pulled from a source tag). Empty in Phase 1.
    entity_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class EntityIP(Base):
    """One IP address known to belong to a :class:`CanonicalEntity`.

    Not unique on ``ip`` alone: two different entities disagreeing about who
    owns an address is a real, if rare, data-quality condition (a reused DHCP
    lease, a misconfigured source) and resolution needs to be able to observe
    it and refuse to guess (:data:`ResolutionMethod.conflict`) rather than the
    schema silently making it impossible to represent. Unique per
    ``(entity_id, ip)`` so re-registering the same address is a no-op, not a
    growing table of duplicate rows.
    """

    __tablename__ = "entity_ips"
    __table_args__ = (
        UniqueConstraint("entity_id", "ip", name="uq_entity_ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EntityAlias(Base):
    """One alternate name known to belong to a :class:`CanonicalEntity`.

    ``alias_type`` separates hostnames, FQDNs, and free-form aliases because
    they are matched at different priority (see
    :func:`app.entity_resolution.resolve_host_entity`) and a bare hostname is
    far more likely to collide across two unrelated entities than an FQDN is.
    Same non-unique-on-value-alone reasoning as :class:`EntityIP`.
    """

    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "alias_type", "alias", name="uq_entity_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    #: hostname | fqdn | alias
    alias_type: Mapped[str] = mapped_column(String(16), index=True)
    #: Stored lowercased/trimmed — matching is always exact-but-case-insensitive.
    alias: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EntityManualMapping(Base):
    """An admin-declared "this source identifier is this entity" override.

    The only kind of mapping that can exist *before* the source has ever
    reported the thing — every other resolution method needs a Host row (or
    an existing entity to match against) to work from. Checked first, ahead
    of every automatic method, which is what "explicit SAMI'X entity mapping"
    ranking above CMDB/IP/hostname matching in the resolution priority means
    in practice: a human's stated answer is never second-guessed by a
    heuristic.
    """

    __tablename__ = "entity_manual_mappings"
    __table_args__ = (
        UniqueConstraint(
            "source_platform", "source_instance", "source_identifier",
            name="uq_entity_manual_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(
        Enum(SourcePlatform, native_enum=False, length=16), index=True
    )
    source_instance: Mapped[str] = mapped_column(String(64), default="")
    #: The source's own identifier — usually a hostname, sometimes the
    #: platform's internal id, whatever an admin was looking at when they
    #: made the mapping.
    source_identifier: Mapped[str] = mapped_column(String(255), index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- Correlation Phase 2: deduplication --------------------------------------
#
# Deduplication is NOT correlation: a LogicalEvent groups occurrences that are
# the SAME deterministic condition recurring or being reported by more than
# one source (Zabbix "CPU utilization is 91%" + Dynatrace "CPU saturation" on
# the same host) — never different problems that merely share a host, a time
# window, or an application. That grouping (temporal/topology/incident
# correlation) is later phases; this table only ever groups occurrences whose
# fingerprint — resolved entity + normalized problem type (+ metric/api/
# service/database/network_device where relevant) — is identical.


class LogicalEvent(Base):
    """A deduplicated group of :class:`Alert` occurrences sharing one
    fingerprint. The occurrences themselves are never deleted or merged —
    each keeps its own source, source_event_id (``external_id``), and
    original_severity; this row only aggregates them for display.

    Every field here is recomputed from its linked occurrences on each touch
    (see ``app.dedup._recompute_logical_events``), never hand-incremented, so
    it can never drift from what a fresh scan of ``Alert`` would show.
    """

    __tablename__ = "logical_events"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_logical_event_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(512), index=True)
    #: Matches CanonicalEntity.id, not a FK — same convention as elsewhere.
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    normalized_problem_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[LogicalEventStatus] = mapped_column(
        Enum(LogicalEventStatus, native_enum=False, length=16),
        default=LogicalEventStatus.open,
        index=True,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    #: Distinct source_platform values among its occurrences, sorted — e.g.
    #: ["dynatrace", "zabbix"] once both have reported the same condition.
    sources: Mapped[list] = mapped_column(JSON, default=list)
    #: Representative fields for display: the highest-severity CURRENTLY
    #: ACTIVE occurrence (falling back to the highest overall once every
    #: occurrence is resolved) — not simply "the latest", so an escalating
    #: group shows its worst active state rather than whichever alert
    #: happened to poll last.
    title: Mapped[str] = mapped_column(String(512), default="")
    current_severity_int: Mapped[int] = mapped_column(Integer, default=1)
    current_severity_label: Mapped[str] = mapped_column(String(32), default="info")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# --- Correlation Phase 3: topology / dependency graph ------------------------
#
# A deterministic graph over CanonicalEntity nodes (Phase 1) — never over raw
# per-platform Host/TopologyNode rows, so "CustomerService depends on
# CustomerDB" means the same thing regardless of which tool originally
# reported either end. This is a SEPARATE graph from the pre-existing
# TopologyNode/TopologyEdge tables (NNMi L2 devices, Dynatrace's raw service
# map) — those record what a platform's own topology API returned, scoped to
# one (platform, instance); this one records resolved, source-attributed
# relationships between canonical entities, several sources deep if they
# disagree. app/topology_sync.py is the bridge: it reads the existing
# TopologyNode/TopologyEdge rows and writes EntityRelationship rows from them,
# it does not replace either table.


class EntityRelationship(Base):
    """One source's claim that ``from_entity`` relates to ``to_entity``.

    Never silently merged with another source's claim about the same pair —
    CMDB and Dynatrace can each record "CustomerService depends_on
    CustomerDB" and both rows persist, each with its own evidence. Re-sending
    the identical claim from the SAME source is idempotent (upsert on the
    unique key below), so re-running a sync job doesn't grow the table.
    """

    __tablename__ = "entity_relationships"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_reference", "from_entity_id", "to_entity_id",
            "relationship_type", name="uq_entity_relationship",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[TopologySource] = mapped_column(
        Enum(TopologySource, native_enum=False, length=16), index=True
    )
    #: Free-text pointer to where this came from (a CMDB CI id, the Dynatrace
    #: relationship's own external id, "declared by <user> via API"). ``""``
    #: (never NULL, same convention as Host.source_instance) for sources that
    #: don't have a natural reference to record, so the unique key above
    #: still holds.
    source_reference: Mapped[str] = mapped_column(String(255), default="")
    relationship_type: Mapped[RelationshipType] = mapped_column(
        Enum(RelationshipType, native_enum=False, length=16), index=True
    )
    #: Both match CanonicalEntity.id — not FKs, same convention as elsewhere.
    from_entity_id: Mapped[int] = mapped_column(Integer, index=True)
    to_entity_id: Mapped[int] = mapped_column(Integer, index=True)
    #: Why this relationship is believed to exist, e.g. "Dynatrace call path
    #: Customer API -> CustomerService" or "CMDB dependency". Shown as-is.
    evidence: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: How much to trust this claim, 0..1. Fixed per source (see
    #: app/topology_sync.py DEFAULT_CONFIDENCE), not derived from the data.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# --- Correlation Phase 4: deterministic correlation rule engine -------------
#
# Decides whether two LogicalEvents (Phase 2) describe the same real-world
# incident — never by AI/ML, only by fixed, auditable signals (entity
# identity, Phase 3 topology, trace ids, timing) evaluated against
# configuration-driven rules. Every correlation records exactly which
# signals fired and why (CorrelationEvidence) — "explainable" is a hard
# requirement here, not a nice-to-have.


class CorrelationRule(Base):
    """A configuration-driven rule: what has to be true between two
    LogicalEvents for them to correlate, and what to do about it.

    ``conditions`` (list[dict], merged into one spec by app.correlation_engine)
    and ``actions`` (dict) are stored as the task's own example shows them —
    a flat list of single-purpose condition objects — rather than a bespoke
    schema, so a rule can be inspected or hand-edited without this class
    changing. See app.correlation_rules for the validation applied at
    creation time (a rule built only from weak signals is rejected).
    """

    __tablename__ = "correlation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: 1 (highest — Exact Trace) .. 5 (lowest — Temporal). Computed from the
    #: rule's own strongest required signal at creation time, not editable
    #: directly — see app.correlation_rules.priority_tier_for.
    priority_tier: Mapped[int] = mapped_column(Integer, index=True)
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    actions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CorrelationWeight(Base):
    """A configurable score weight for one signal. Seeded with the defaults
    in app.correlation_engine.DEFAULT_WEIGHTS on first use; editable via
    ``PUT /api/v1/correlation/weights`` without a restart.
    """

    __tablename__ = "correlation_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal: Mapped[CorrelationSignal] = mapped_column(
        Enum(CorrelationSignal, native_enum=False, length=32), unique=True
    )
    weight: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Correlation(Base):
    """A group of 2+ LogicalEvents a rule determined describe the same
    real-world incident. Never created on weak evidence alone (temporal/
    same_host/same_application/multi_source) — see the module note above.

    ``member_event_ids`` is the authoritative membership list (LogicalEvent
    ids); ``score``/``status``/``correlation_type`` are recomputed from it
    and its evidence on every touch, not hand-maintained.
    """

    __tablename__ = "correlations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_type: Mapped[CorrelationType] = mapped_column(
        Enum(CorrelationType, native_enum=False, length=24), index=True
    )
    status: Mapped[CorrelationStatus] = mapped_column(
        Enum(CorrelationStatus, native_enum=False, length=16),
        default=CorrelationStatus.new,
        index=True,
    )
    #: The rule_id (app.models.CorrelationRule.rule_id) that most recently
    #: (re)confirmed this correlation — not necessarily the one that first
    #: created it, since a stronger rule can supersede a weaker one on
    #: re-evaluation. Null if the correlation's rule was since deleted.
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    #: LogicalEvent ids in this group — the highest-priority-tier member
    #: (see app.correlation_engine) is index 0 by convention, e.g. the
    #: network switch in a "switch down + N unreachable hosts" correlation.
    member_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CorrelationEvidence(Base):
    """One fact supporting a :class:`Correlation` — exactly what the task's
    "Correlation Evidence" section asks for: which signal, what its value
    was, where it came from, when it was observed, and which event/entity
    it concerns. A correlation with two contributing signals has two of
    these rows, each independently inspectable.
    """

    __tablename__ = "correlation_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[int] = mapped_column(Integer, index=True)
    signal: Mapped[CorrelationSignal] = mapped_column(
        Enum(CorrelationSignal, native_enum=False, length=32), index=True
    )
    #: Human-readable value, e.g. "CustomerDB" (same_database), "312s apart"
    #: (temporal_relationship), or a trace id (same_trace).
    value: Mapped[str] = mapped_column(String(500), default="")
    #: Where this evidence came from: a TopologySource value for
    #: known_dependency, "phase2" for signals read off LogicalEvent/Alert,
    #: or "engine" for computed ones like temporal_relationship.
    source: Mapped[str] = mapped_column(String(32), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    #: The OTHER LogicalEvent this evidence relates the correlation's primary
    #: member to (a pairwise fact), and the entity involved, if any.
    related_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    related_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- Correlation Phase 5: application architecture + incidents ---------------
#
# Phase 4 decides whether two LogicalEvents are the same real-world problem.
# Phase 5 is the layer above that: turning one or more Correlation groups into
# a first-class Incident, ranking which of its member entities is most likely
# the actual cause (never claimed as certain — see RootCauseCandidate's own
# note), and rolling application-level health up from its individual APIs
# rather than treating one failing endpoint as the whole application down.
# See app/incident_engine.py, app/application_health.py, app/trace_ingest.py.


class SymptomRole(str, enum.Enum):
    """How one LogicalEvent relates to the Incident it's classified under.

    - ``primary``: on a root_cause_candidate entity — the event(s) closest to
      where the incident actually originated, per current evidence.
    - ``related_symptom``: correlated into the same incident, but on a
      different (downstream/affected) entity.
    - ``independent``: evaluated as a candidate but NOT included — kept for
      explainability (why something that looked related was ruled out), never
      as a reason to delete or hide the event itself.
    """

    primary = "primary"
    related_symptom = "related_symptom"
    independent = "independent"


class IncidentStatus(str, enum.Enum):
    """An :class:`Incident`'s lifecycle state, recomputed from its current
    membership every time it's touched — same "pure function of current
    data" philosophy as LogicalEventStatus/CorrelationStatus.

    - ``open``: active, steady state.
    - ``updated``: membership grew this run.
    - ``resolved``: every member LogicalEvent is resolved.
    - ``reopened``: was resolved, a new/active member has since joined.
    - ``merged``: absorbed into another Incident (see ``merged_into_id``) —
      terminal; kept in place rather than deleted, per "never delete
      symptoms".
    """

    open = "open"
    updated = "updated"
    resolved = "resolved"
    reopened = "reopened"
    merged = "merged"


class Incident(Base):
    """A named, correlated real-world problem — one or more Phase 4
    :class:`Correlation` groups, elevated to something a human (or another
    system) can track: a title, a severity, a status, who/what it affects,
    and — where the evidence actually supports it — which entity most likely
    caused it.

    Every summary field here (``severity_*``, ``status``, ``affected``,
    ``root_cause_candidates``, ``member_roles``, ``correlation_types``) is
    recomputed from ``related_events`` and ``source_correlation_ids`` on
    every touch by app.incident_engine.recompute_incident — never
    hand-maintained, so it can't drift from what those groups currently show.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, native_enum=False, length=16),
        default=IncidentStatus.open,
        index=True,
    )
    #: Worst CURRENTLY ACTIVE member's severity (falls back to worst overall
    #: once every member is resolved) — same convention as LogicalEvent's.
    severity_int: Mapped[int] = mapped_column(Integer, default=1)
    severity_label: Mapped[str] = mapped_column(String(32), default="info")
    #: Earliest first_seen among every member LogicalEvent — when this
    #: real-world problem actually began, not when SAMI'X noticed it.
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: A single business_service label when every member event agrees on one
    #: (Alert.business_service); null when they disagree or none carry one —
    #: never guessed at.
    business_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which Phase 4 Correlation groups this incident was built from — more
    #: than one after a deliberate merge (see app.incident_engine.merge_incidents).
    source_correlation_ids: Mapped[list] = mapped_column(JSON, default=list)
    #: The authoritative membership list (LogicalEvent ids) — union of every
    #: source correlation's own member_event_ids.
    related_events: Mapped[list] = mapped_column(JSON, default=list)
    #: Distinct CorrelationType values seen across every contributing
    #: correlation's evidence — what KINDS of evidence this incident rests on.
    correlation_types: Mapped[list] = mapped_column(JSON, default=list)
    #: {"applications": [...], "services": [...], "apis": [...],
    #:  "databases": [...], "hosts": [...], "network_devices": [...]} — each
    #: entry {"entity_id": int, "canonical_name": str}. One JSON column
    #: rather than six, unpacked into the six named fields the task asks for
    #: at the API layer (see schemas.IncidentOut).
    affected: Mapped[dict] = mapped_column(JSON, default=dict)
    #: [{"entity_id", "entity_type", "canonical_name", "evidence": [str,...],
    #:   "evidence_count": int}, ...] — every entity the evidence points to as
    #: a POSSIBLE origin, ranked, never collapsed to one claimed-certain
    #: cause. See app.incident_engine.root_cause_candidates.
    root_cause_candidates: Mapped[list] = mapped_column(JSON, default=list)
    #: {str(event_id): SymptomRole.value} for every member in related_events.
    member_roles: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Set when status == merged: the surviving Incident's id. This row is
    #: kept, not deleted — see IncidentStatus.merged.
    merged_into_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
