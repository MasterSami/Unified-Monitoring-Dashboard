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
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class Alert(Base):
    """An alert / problem / incident, normalized across all platforms."""

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
