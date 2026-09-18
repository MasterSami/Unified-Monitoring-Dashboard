"""Pydantic schemas for the JSON API and internal data transfer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HostOut(BaseModel):
    """Serialized host record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    hostname: str
    ip: str | None
    source_platform: str
    source_instance: str
    external_id: str
    status: str
    group_name: str | None
    last_seen: datetime | None
    updated_at: datetime


class AlertOut(BaseModel):
    """Serialized alert record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    source_platform: str
    source_instance: str
    host_hostname: str | None
    severity_int: int
    severity_label: str
    title: str
    started_at: datetime | None
    resolved: bool
    updated_at: datetime


class EventOut(BaseModel):
    """A canonical monitoring event (Correlation Phase 1) — the full
    Alert row, including source identity, resolution info, and whatever
    canonical fields the source's adapter and entity resolution filled in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    # --- source (never destroyed) -------------------------------------
    external_id: str
    source_platform: str
    source_instance: str
    original_severity: str | None
    original_description: str | None
    # --- current state ---------------------------------------------------
    status: str | None
    severity_int: int
    severity_label: str
    title: str
    started_at: datetime | None
    last_seen: datetime | None
    resolved: bool
    event_type: str | None
    problem_type: str | None
    tags: list = Field(default_factory=list)
    # --- host / entity context -------------------------------------------
    host_hostname: str | None
    host_external_id: str | None
    host_ip: str | None
    fqdn: str | None
    host_group: str | None
    host_owner: str | None
    entity_id: int | None
    entity_type: str | None
    # --- resolution info ---------------------------------------------------
    # (the *how* lives on the matched Host row, surfaced via /entities and
    # /entity-mappings; repeated here would drift from it on every poll)
    # --- broader canonical context (mostly null until a later phase
    # populates them — see app/canonical_event.py) -------------------------
    application_id: int | None
    application_name: str | None
    service_id: int | None
    service_name: str | None
    api_id: int | None
    api_name: str | None
    endpoint: str | None
    http_method: str | None
    database_id: int | None
    database_name: str | None
    network_device_id: int | None
    network_device_name: str | None
    metric_name: str | None
    metric_value: float | None
    metric_unit: str | None
    threshold: float | None
    environment: str | None
    location: str | None
    business_service: str | None
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    payload_ref: str | None
    updated_at: datetime


class EntitySourceRef(BaseModel):
    """One source's view of a :class:`~app.models.CanonicalEntity`."""

    source_platform: str
    source_instance: str
    external_id: str
    hostname: str
    resolution_method: str | None
    resolution_confidence: float | None


class EntityOut(BaseModel):
    """A resolved canonical entity, with its known aliases/IPs/sources."""

    entity_id: int
    entity_type: str
    canonical_name: str
    cmdb_id: str | None
    aliases: list[str] = Field(default_factory=list)
    ips: list[str] = Field(default_factory=list)
    source_entities: list[EntitySourceRef] = Field(default_factory=list)


class EntityMappingOut(BaseModel):
    """One row of ``source identifier -> canonical entity``, however it was
    established — a manual override or an automatic resolution.
    """

    source_platform: str
    source_instance: str
    source_identifier: str
    hostname: str | None
    entity_id: int
    resolution_method: str
    resolution_confidence: float | None
    is_manual: bool


class EntityMappingIn(BaseModel):
    """Declare an explicit ``source identifier -> entity`` mapping.

    ``entity_id`` targets an existing entity; omit it and pass
    ``entity_type``/``canonical_name`` to create a new one in the same call
    (useful for pre-declaring an identity before the source has ever
    reported it).
    """

    source_platform: str
    source_instance: str = ""
    source_identifier: str = Field(min_length=1)
    entity_id: int | None = None
    entity_type: str = "host"
    canonical_name: str | None = None
    note: str | None = None


class LogicalEventOccurrenceOut(BaseModel):
    """One source occurrence linked to a :class:`LogicalEventOut` — never
    merged or deleted, always its own source/source_event_id/original_severity.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_platform: str
    source_instance: str
    external_id: str
    original_severity: str | None
    severity_label: str
    title: str
    host_hostname: str | None
    started_at: datetime | None
    resolved: bool
    status: str | None


class LogicalEventOut(BaseModel):
    """A deduplicated group of occurrences sharing one fingerprint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fingerprint: str
    entity_id: int | None
    normalized_problem_type: str
    status: str
    occurrence_count: int
    first_seen: datetime | None
    last_seen: datetime | None
    sources: list[str] = Field(default_factory=list)
    title: str
    current_severity_int: int
    current_severity_label: str
    created_at: datetime
    updated_at: datetime


class RelationshipOut(BaseModel):
    """One EntityRelationship row, as stored — one source's claim."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    source_reference: str
    relationship_type: str
    from_entity_id: int
    to_entity_id: int
    evidence: str | None
    confidence: float | None
    created_at: datetime
    updated_at: datetime


class RelationshipIn(BaseModel):
    """Declare a relationship between two existing canonical entities.

    ``source`` should be ``manual``, ``cmdb``, or ``explicit_config`` for
    anything entered this way — ``dynatrace``/``monitoring`` are written
    only by app.topology_sync, from data this app actually observed.
    """

    source: str = Field(default="manual")
    source_reference: str = ""
    relationship_type: str
    from_entity_id: int
    to_entity_id: int
    evidence: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class RelatedEntityOut(BaseModel):
    """One neighbor in a one-hop view (app.dependency_graph.direct_relationships)."""

    relationship_id: int
    entity_id: int
    entity_type: str | None
    canonical_name: str | None
    relationship_type: str
    source: str
    evidence: str | None
    confidence: float | None


class DirectRelationshipsOut(BaseModel):
    """One entity's immediate neighbors, both directions, as stored."""

    entity_id: int
    outgoing: list[RelatedEntityOut] = Field(default_factory=list)
    incoming: list[RelatedEntityOut] = Field(default_factory=list)


class DependencyNodeOut(BaseModel):
    """One entity reached during a traversal — see app.dependency_graph."""

    entity_id: int
    entity_type: str
    canonical_name: str
    depth: int
    relationship_type: str
    source: str
    via_entity_id: int
    path: list[int] = Field(default_factory=list)
    #: True if this entity has an open/deduplicated/reopened LogicalEvent
    #: (Phase 2) right now — real data, not a guess. Filled in by the API
    #: route (app.dependency_graph itself has no Phase 2 dependency), so the
    #: UI can tell a healthy node from one with an active issue.
    has_active_issue: bool = False


class TraversalOut(BaseModel):
    """The result of a dependency/impact traversal."""

    root_entity_id: int
    root_entity_type: str
    root_canonical_name: str
    root_has_active_issue: bool = False
    direction: str
    nodes: list[DependencyNodeOut] = Field(default_factory=list)
    max_depth: int
    truncated: bool
    cycle_detected: bool


class PathOut(BaseModel):
    """A path between two entities, or none if unreachable within bounds."""

    from_entity_id: int
    to_entity_id: int
    found: bool
    hops: list[DependencyNodeOut] = Field(default_factory=list)


class CorrelationRuleIn(BaseModel):
    """Declare or update a correlation rule (Phase 4).

    ``conditions`` follows the task's own example shape: a list of
    single-purpose objects, e.g. ``[{"event_type": "NETWORK_DEVICE_DOWN"},
    {"relationship": "affects"}, {"time_window_seconds": 300}]``. Rejected
    (422) if it has no ``time_window_seconds`` or if its required signals
    are all "weak" ones (temporal_relationship/same_host/same_application/
    multi_source) — see app.correlation_rules.validate_rule_conditions.
    """

    rule_id: str = Field(min_length=1, max_length=64)
    name: str = ""
    enabled: bool = True
    conditions: list[dict] = Field(min_length=1)
    actions: dict = Field(default_factory=dict)


class CorrelationRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: str
    name: str
    enabled: bool
    priority_tier: int
    conditions: list
    actions: dict
    created_at: datetime
    updated_at: datetime


class CorrelationWeightOut(BaseModel):
    signal: str
    weight: float


class SignalHitOut(BaseModel):
    signal: str
    value: str
    source: str
    priority_tier: int
    related_entity_id: int | None = None


class CorrelationOutcomeOut(BaseModel):
    """The full, explainable result of evaluating one pair of events —
    mirrors app.correlation_engine.CorrelationOutcome.
    """

    correlated: bool
    reason: str
    hits: list[SignalHitOut] = Field(default_factory=list)
    score: float = 0.0
    correlation_type: str | None = None
    matched_rule_id: str | None = None
    correlation_id: int | None = None
    status: str | None = None


class CorrelationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    correlation_type: str
    status: str
    rule_id: str | None
    score: float
    member_event_ids: list[int] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CorrelationEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    signal: str
    value: str
    source: str
    timestamp: datetime
    related_event_id: int | None
    related_entity_id: int | None


class CorrelationDetailOut(CorrelationOut):
    evidence: list[CorrelationEvidenceOut] = Field(default_factory=list)


class TraceSpanIn(BaseModel):
    """One hop of one distributed trace — Correlation Phase 5. All fields
    but ``trace_id``/``span_id`` are optional; declare whichever layers this
    span actually names (see app.trace_ingest for exactly what each implies).
    """

    trace_id: str = Field(min_length=1, max_length=64)
    span_id: str = Field(min_length=1, max_length=64)
    parent_span_id: str | None = None
    application: str | None = None
    service: str | None = None
    api: str | None = None
    endpoint: str | None = None
    http_method: str | None = None
    database: str | None = None
    external_service: str | None = None
    business_transaction: str | None = None
    business_service: str | None = None
    duration_ms: float | None = None
    status_code: int | None = None
    error: bool = False
    #: A later span for the same (trace_id, span_id) marking a prior
    #: error resolved — see app.trace_ingest's module note on recovery.
    resolved: bool = False
    started_at: datetime | None = None


class TraceIngestIn(BaseModel):
    source_instance: str = Field(min_length=1, max_length=64)
    spans: list[TraceSpanIn] = Field(min_length=1)


class TraceIngestResultOut(BaseModel):
    received: int
    inserted: int
    updated: int
    resolved: int
    relationships_declared: int


class RootCauseCandidateOut(BaseModel):
    """One POSSIBLE origin of an incident, with the evidence for it — never
    a single claimed-certain cause. See app.incident_engine.root_cause_candidates.
    """

    entity_id: int
    entity_type: str
    canonical_name: str
    evidence: list[str] = Field(default_factory=list)
    evidence_count: int


class AffectedEntityOut(BaseModel):
    entity_id: int
    canonical_name: str


class IncidentAffectedOut(BaseModel):
    applications: list[AffectedEntityOut] = Field(default_factory=list)
    services: list[AffectedEntityOut] = Field(default_factory=list)
    apis: list[AffectedEntityOut] = Field(default_factory=list)
    databases: list[AffectedEntityOut] = Field(default_factory=list)
    hosts: list[AffectedEntityOut] = Field(default_factory=list)
    network_devices: list[AffectedEntityOut] = Field(default_factory=list)


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    status: str
    severity_int: int
    severity_label: str
    start_time: datetime | None
    last_update: datetime
    business_service: str | None
    related_events: list[int] = Field(default_factory=list)
    correlation_types: list[str] = Field(default_factory=list)
    source_correlation_ids: list[int] = Field(default_factory=list)
    affected: IncidentAffectedOut
    root_cause_candidates: list[RootCauseCandidateOut] = Field(default_factory=list)
    member_roles: dict[str, str] = Field(default_factory=dict)
    merged_into_id: int | None = None
    created_at: datetime


class IncidentTimelineEntryOut(BaseModel):
    timestamp: datetime
    event_id: int
    entity_id: int | None
    entity_name: str
    description: str
    #: "onset" | "recovery"
    kind: str
    source_platform: str


class IncidentEvidenceEntryOut(BaseModel):
    correlation_id: int
    signal: str
    value: str
    source: str
    timestamp: datetime
    related_event_id: int | None
    related_entity_id: int | None


class IncidentDetailOut(IncidentOut):
    timeline: list[IncidentTimelineEntryOut] = Field(default_factory=list)
    evidence: list[IncidentEvidenceEntryOut] = Field(default_factory=list)


class ApplicationHealthOut(BaseModel):
    application_entity_id: int
    application_name: str
    #: "healthy" | "degraded" | "down"
    status: str
    total_apis: int
    affected_apis: list[dict] = Field(default_factory=list)
    healthy_apis: list[dict] = Field(default_factory=list)


class IncidentImpactOut(BaseModel):
    incident_id: int
    affected: IncidentAffectedOut
    business_service: str | None
    #: One entry per affected application actually reachable in the
    #: dependency graph from an incident member — see app.application_health.
    application_health: list[ApplicationHealthOut] = Field(default_factory=list)


class IncidentGraphNodeOut(BaseModel):
    entity_id: int
    entity_type: str
    canonical_name: str
    #: "root_cause_candidate" | "member"
    role: str
    logical_event_ids: list[int] = Field(default_factory=list)


class IncidentGraphEdgeOut(BaseModel):
    from_entity_id: int
    to_entity_id: int
    relationship_type: str
    source: str


class IncidentCorrelationGraphOut(BaseModel):
    incident_id: int
    nodes: list[IncidentGraphNodeOut] = Field(default_factory=list)
    edges: list[IncidentGraphEdgeOut] = Field(default_factory=list)


class IncidentMergeIn(BaseModel):
    incident_ids: list[int] = Field(min_length=2)


class IncidentSplitIn(BaseModel):
    event_ids: list[int] = Field(min_length=1)


class CollectorStatus(BaseModel):
    """Health snapshot for a single collector instance."""

    platform: str
    instance: str
    enabled: bool
    last_run_at: datetime | None
    last_success_at: datetime | None
    status: str  # success | failed | never | disabled
    items_collected: int
    hosts_collected: int = 0
    alerts_collected: int = 0
    error_message: str | None
    notes: str | None = None
    test_mail: bool = False
    check_proxies: bool = False


class SeverityBucket(BaseModel):
    """Active alert count for a single severity level."""

    severity_int: int
    label: str
    count: int


class PlatformHostCount(BaseModel):
    """Host counts for a single platform, split by status.

    ``total`` is what the platform actually monitors; ``discovered`` is
    everything it knows about. The two differ only for Dynatrace, whose entity
    API also returns hosts seen as network peers with no OneAgent installed.

    The status fields sum to ``total``. They are carried separately because
    deriving "up" as ``total - down`` counts every ``unknown`` and ``disabled``
    host as healthy — which on a real estate is most of the difference.
    """

    platform: str
    total: int
    down: int
    up: int = 0
    unknown: int = 0
    disabled: int = 0
    discovered: int = 0


class SummaryOut(BaseModel):
    """Aggregate dashboard summary."""

    total_hosts: int
    hosts_down: int
    active_alerts: int
    per_platform: list[PlatformHostCount]
    severity_buckets: list[SeverityBucket]
    collectors: list[CollectorStatus]


class SiteScopeIngest(BaseModel):
    """Push payload from the SiteScope forwarder.

    ``lines`` are **already-redacted** tab-delimited log lines (the forwarder
    redacts on the SiteScope box before transmission). The UMD parses and
    normalizes them, and redacts again as a safety net. A heartbeat with an empty
    ``lines`` list keeps the collector marked alive between real events.
    """

    source_instance: str = Field(min_length=1, max_length=64)
    heartbeat: bool = False
    lines: list[str] = Field(default_factory=list)


class IngestResult(BaseModel):
    """Result of an ingest request."""

    status: str
    received: int
    inserted: int
    updated: int
    skipped: int
    redactions: int
