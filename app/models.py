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

    Phase 1 (entity resolution) only ever creates ``host`` entities — that is
    the only kind any current collector can resolve deterministically. The
    rest exist so the schema does not need to change again when a later
    Correlation phase learns to resolve services/applications/etc from the
    topology graph.
    """

    host = "host"
    network_device = "network_device"
    application = "application"
    service = "service"
    api = "api"
    database = "database"
    external_service = "external_service"
    business_service = "business_service"


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
