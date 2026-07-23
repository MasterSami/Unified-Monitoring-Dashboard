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
    """Supported monitoring source platforms."""

    zabbix = "zabbix"
    dynatrace = "dynatrace"
    nnmi = "nnmi"


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
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    severity_int: Mapped[int] = mapped_column(Integer, default=1, index=True)
    severity_label: Mapped[str] = mapped_column(String(32), default="info")
    title: Mapped[str] = mapped_column(String(512), default="")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
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
