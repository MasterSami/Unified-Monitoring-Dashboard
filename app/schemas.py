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
