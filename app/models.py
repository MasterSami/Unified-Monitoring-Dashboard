"""SQLAlchemy ORM models for the unified monitoring data model.

Three platforms (Zabbix, Dynatrace, NNMi) are normalized into a shared
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

    The first four are monitoring tools that report live state. ``huawei`` is
    different in kind: it is the Huawei i2000 / Digital View **asset
    inventory**, loaded from an exported workbook because the API port is
    closed to us. It tells us what exists and how big it is, never whether it
    is up — so its hosts carry ``unknown`` status by design.
    """

    zabbix = "zabbix"
    dynatrace = "dynatrace"
    nnmi = "nnmi"
    sitescope = "sitescope"
    huawei = "huawei"


#: Every platform, in the order the UI lists them. Monitoring tools first,
#: inventory sources last.
PLATFORM_ORDER: tuple[str, ...] = (
    "zabbix", "dynatrace", "nnmi", "sitescope", "huawei",
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
