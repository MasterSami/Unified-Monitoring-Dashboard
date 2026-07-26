"""JSON API under ``/api/v1``.

These endpoints back the UI and are the integration surface for later reports
and automation.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Alert, Host, HostStatus
from app.normalizer import severity_label
from app.scheduler import get_collector_statuses, get_service
from app.schemas import (
    AlertOut,
    CollectorStatus,
    HostOut,
    PlatformHostCount,
    SeverityBucket,
    SummaryOut,
)

router = APIRouter(prefix="/api/v1", tags=["api"])


@router.get("/hosts", response_model=list[HostOut])
def list_hosts(
    platform: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Host]:
    """Return hosts, optionally filtered by platform, status, or search text."""
    stmt = select(Host)
    if platform:
        stmt = stmt.where(Host.source_platform == platform)
    if status:
        stmt = stmt.where(Host.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Host.hostname).like(like) | func.lower(Host.ip).like(like)
        )
    stmt = stmt.order_by(Host.hostname.asc())
    return list(db.scalars(stmt).all())


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    active: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> list[Alert]:
    """Return alerts. ``active=true`` (default) excludes resolved alerts."""
    stmt = select(Alert)
    if active:
        stmt = stmt.where(Alert.resolved.is_(False))
    stmt = stmt.order_by(
        Alert.severity_int.desc(), Alert.started_at.desc().nullslast()
    )
    return list(db.scalars(stmt).all())


def _csv_response(filename: str, header: list[str], rows: list[list]) -> StreamingResponse:
    """Serialize rows to a downloadable CSV StreamingResponse."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/hosts.csv")
def export_hosts_csv(
    platform: str | None = Query(default=None),
    instance: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export hosts (respecting filters) as CSV."""
    stmt = select(Host)
    if platform and platform != "all":
        stmt = stmt.where(Host.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(Host.source_instance == instance)
    if status and status != "all":
        stmt = stmt.where(Host.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Host.hostname).like(like)
            | func.lower(func.coalesce(Host.ip, "")).like(like)
            | func.lower(func.coalesce(Host.group_name, "")).like(like)
            | func.lower(func.coalesce(Host.source_instance, "")).like(like)
        )
    stmt = stmt.order_by(Host.hostname.asc())
    rows = [
        [
            h.hostname,
            h.ip or "",
            h.source_platform.value,
            h.source_instance,
            h.status.value,
            h.group_name or "",
            h.last_seen.isoformat() if h.last_seen else "",
        ]
        for h in db.scalars(stmt).all()
    ]
    return _csv_response(
        "hosts.csv",
        ["hostname", "ip", "platform", "instance", "status", "group", "last_seen"],
        rows,
    )


@router.get("/alerts.csv")
def export_alerts_csv(
    active: bool = Query(default=True),
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export alerts (respecting filters) as CSV."""
    stmt = select(Alert)
    if active:
        stmt = stmt.where(Alert.resolved.is_(False))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Alert.title).like(like)
            | func.lower(func.coalesce(Alert.host_hostname, "")).like(like)
            | func.lower(func.coalesce(Alert.source_instance, "")).like(like)
            | func.lower(Alert.source_platform).like(like)
            | func.lower(Alert.severity_label).like(like)
        )
    stmt = stmt.order_by(
        Alert.severity_int.desc(), Alert.started_at.desc().nullslast()
    )
    rows = [
        [
            a.severity_int,
            a.severity_label,
            a.title,
            a.source_platform.value,
            a.source_instance,
            a.host_hostname or "",
            a.started_at.isoformat() if a.started_at else "",
            "resolved" if a.resolved else "active",
        ]
        for a in db.scalars(stmt).all()
    ]
    return _csv_response(
        "alerts.csv",
        [
            "severity_int",
            "severity_label",
            "title",
            "platform",
            "instance",
            "host",
            "started_at",
            "state",
        ],
        rows,
    )


@router.get("/summary", response_model=SummaryOut)
def summary(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SummaryOut:
    """Return aggregate KPIs for the overview page."""
    total_hosts = db.scalar(select(func.count(Host.id))) or 0
    hosts_down = (
        db.scalar(
            select(func.count(Host.id)).where(Host.status == HostStatus.down)
        )
        or 0
    )
    active_alerts = (
        db.scalar(select(func.count(Alert.id)).where(Alert.resolved.is_(False)))
        or 0
    )

    # Per-platform host totals and down counts (two small grouped queries).
    def _by_platform(rows: list[tuple]) -> dict[str, int]:
        return {getattr(p, "value", str(p)): int(c) for p, c in rows}

    total_by_platform = _by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id)).group_by(
                Host.source_platform
            )
        ).all()
    )
    down_by_platform = _by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id))
            .where(Host.status == HostStatus.down)
            .group_by(Host.source_platform)
        ).all()
    )
    per_platform = [
        PlatformHostCount(
            platform=plat,
            total=total_by_platform.get(plat, 0),
            down=down_by_platform.get(plat, 0),
        )
        for plat in ("zabbix", "dynatrace", "nnmi")
    ]

    # Active alerts by severity.
    sev_rows = db.execute(
        select(Alert.severity_int, func.count(Alert.id))
        .where(Alert.resolved.is_(False))
        .group_by(Alert.severity_int)
    ).all()
    sev_counts = {int(s): int(c) for s, c in sev_rows}
    severity_buckets = [
        SeverityBucket(
            severity_int=level,
            label=severity_label(level),
            count=sev_counts.get(level, 0),
        )
        for level in (5, 4, 3, 2, 1)
    ]

    collectors = get_collector_statuses(db, settings)

    return SummaryOut(
        total_hosts=total_hosts,
        hosts_down=hosts_down,
        active_alerts=active_alerts,
        per_platform=per_platform,
        severity_buckets=severity_buckets,
        collectors=collectors,
    )


@router.get("/collectors/status", response_model=list[CollectorStatus])
def collectors_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[CollectorStatus]:
    """Return current health for every configured instance."""
    return get_collector_statuses(db, settings)


@router.post("/collectors/{instance}/run")
def run_collector(instance: str) -> dict[str, str]:
    """Manually trigger a single instance's collection run (synchronous)."""
    service = get_service()
    ran = service.run_one(instance)
    if not ran:
        raise HTTPException(status_code=404, detail=f"unknown instance: {instance}")
    return {"status": "ok", "instance": instance}


@router.post("/collectors/run")
def run_all_collectors() -> dict[str, object]:
    """Trigger a run of every configured instance (synchronous)."""
    service = get_service()
    service.run_all()
    return {"status": "ok", "instances": list(service.collectors)}


@router.post("/collectors/{instance}/test-mail")
def test_mail(
    instance: str,
    to: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Ask a Zabbix instance to send a test email (verifies email alerting)."""
    service = get_service()
    collector = service.get(instance)
    if collector is None or not hasattr(collector, "send_test_mail"):
        raise HTTPException(
            status_code=404,
            detail=f"instance '{instance}' not found or does not support test mail",
        )
    recipient = to or settings.test_mail_to
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail="no recipient: pass ?to=... or set TEST_MAIL_TO in .env",
        )
    result = collector.send_test_mail(recipient)  # type: ignore[attr-defined]
    return {"instance": instance, "to": recipient, **result}
