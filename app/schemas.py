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
