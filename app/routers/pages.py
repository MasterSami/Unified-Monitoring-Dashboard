"""HTML pages (Jinja2) and HTMX partial fragments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Alert, Host, HostStatus
from app.normalizer import severity_label
from app.scheduler import get_collector_statuses
from app.schemas import PlatformHostCount, SeverityBucket

router = APIRouter(tags=["pages"])

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


# --- Template helpers -------------------------------------------------------


def _relative_time(value: datetime | None) -> str:
    """Render a datetime as a compact 'x ago' string."""
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


templates.env.filters["relative_time"] = _relative_time


# --- Shared query helpers ---------------------------------------------------


def _severity_buckets(db: Session) -> list[SeverityBucket]:
    rows = db.execute(
        select(Alert.severity_int, func.count(Alert.id))
        .where(Alert.resolved.is_(False))
        .group_by(Alert.severity_int)
    ).all()
    counts = {int(s): int(c) for s, c in rows}
    return [
        SeverityBucket(
            severity_int=level, label=severity_label(level), count=counts.get(level, 0)
        )
        for level in (5, 4, 3, 2, 1)
    ]


def _per_platform(db: Session) -> list[PlatformHostCount]:
    def by_platform(rows: list[tuple]) -> dict[str, int]:
        return {getattr(p, "value", str(p)): int(c) for p, c in rows}

    totals = by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id)).group_by(
                Host.source_platform
            )
        ).all()
    )
    downs = by_platform(
        db.execute(
            select(Host.source_platform, func.count(Host.id))
            .where(Host.status == HostStatus.down)
            .group_by(Host.source_platform)
        ).all()
    )
    return [
        PlatformHostCount(
            platform=p, total=totals.get(p, 0), down=downs.get(p, 0)
        )
        for p in ("zabbix", "dynatrace", "nnmi")
    ]


def _donut_gradient(buckets: list[SeverityBucket]) -> str:
    """Build a CSS conic-gradient stop list for the severity donut."""
    total = sum(b.count for b in buckets)
    if total == 0:
        return "var(--surface-3) 0 100%"
    stops: list[str] = []
    acc = 0.0
    for b in buckets:
        if b.count == 0:
            continue
        start = acc / total * 100
        acc += b.count
        end = acc / total * 100
        stops.append(f"var(--sev{b.severity_int}) {start:.3f}% {end:.3f}%")
    return ", ".join(stops)


def _host_status_counts(db: Session) -> dict[str, int]:
    """Return host counts keyed by normalized status (up/down/unknown/disabled)."""
    rows = db.execute(
        select(Host.status, func.count(Host.id)).group_by(Host.status)
    ).all()
    counts = {s.value if hasattr(s, "value") else str(s): int(c) for s, c in rows}
    return {
        "up": counts.get("up", 0),
        "down": counts.get("down", 0),
        "unknown": counts.get("unknown", 0),
        "disabled": counts.get("disabled", 0),
    }


def _shared_host_groups(db: Session) -> list[dict]:
    """Group hosts that share an IP across 2+ instances.

    The same physical device monitored by more than one platform/instance shows
    up as several :class:`Host` rows with the same IP. Grouping by IP surfaces
    that overlap (e.g. a node on both Zabbix-34 and Zabbix-67, or on Zabbix and
    NNMi at once).
    """
    hosts = list(
        db.scalars(
            select(Host).where(Host.ip.isnot(None), Host.ip != "")
        ).all()
    )
    by_ip: dict[str, list[Host]] = {}
    for h in hosts:
        by_ip.setdefault(str(h.ip), []).append(h)

    groups: list[dict] = []
    for ip, members in by_ip.items():
        instances = {(m.source_platform, m.source_instance) for m in members}
        if len(instances) < 2:
            continue
        members = sorted(
            members,
            key=lambda m: (m.source_platform.value, m.source_instance),
        )
        groups.append(
            {
                "ip": ip,
                "hostname": members[0].hostname,
                "members": members,
                "instance_count": len(instances),
                "platforms": sorted({m.source_platform.value for m in members}),
            }
        )
    groups.sort(key=lambda g: (-g["instance_count"], g["ip"]))
    return groups


def _shared_hosts_count(db: Session) -> int:
    """Number of devices monitored by more than one instance."""
    return len(_shared_host_groups(db))


def _recent_critical(db: Session, limit: int = 6) -> list[Alert]:
    stmt = (
        select(Alert)
        .where(Alert.resolved.is_(False))
        .order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def _overview_context(db: Session, settings: Settings) -> dict:
    total_hosts = db.scalar(select(func.count(Host.id))) or 0
    hosts_down = (
        db.scalar(select(func.count(Host.id)).where(Host.status == HostStatus.down))
        or 0
    )
    active_alerts = (
        db.scalar(select(func.count(Alert.id)).where(Alert.resolved.is_(False))) or 0
    )
    buckets = _severity_buckets(db)
    per_platform = _per_platform(db)
    max_total = max((p.total for p in per_platform), default=0) or 1
    status_counts = _host_status_counts(db)
    return {
        "total_hosts": total_hosts,
        "status_counts": status_counts,
        "hosts_up": status_counts["up"],
        "hosts_down": status_counts["down"],
        "active_alerts": active_alerts,
        "per_platform": per_platform,
        "platform_max": max_total,
        "severity_buckets": buckets,
        "donut_gradient": _donut_gradient(buckets),
        "recent_critical": _recent_critical(db),
        "shared_count": _shared_hosts_count(db),
        "collectors": get_collector_statuses(db, settings),
    }


def _hosts_query(
    db: Session,
    q: str | None,
    platform: str | None,
    status: str | None,
    sort: str,
    order: str,
) -> list[Host]:
    stmt = select(Host)
    if platform and platform != "all":
        stmt = stmt.where(Host.source_platform == platform)
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
    sort_cols = {
        "hostname": Host.hostname,
        "ip": Host.ip,
        "platform": Host.source_platform,
        "instance": Host.source_instance,
        "status": Host.status,
        "group": Host.group_name,
        "last_seen": Host.last_seen,
    }
    col = sort_cols.get(sort, Host.hostname)
    stmt = stmt.order_by(col.desc() if order == "desc" else col.asc())
    return list(db.scalars(stmt).all())


def _active_alerts(db: Session) -> list[Alert]:
    stmt = (
        select(Alert)
        .where(Alert.resolved.is_(False))
        .order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
    )
    return list(db.scalars(stmt).all())


# --- Full pages -------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def overview(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Overview dashboard with KPI cards."""
    ctx = _overview_context(db, settings)
    ctx.update({"request": request, "active_page": "overview"})
    return templates.TemplateResponse("overview.html", ctx)


@router.get("/hosts", response_class=HTMLResponse)
def hosts_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Unified hosts table page."""
    hosts = _hosts_query(db, None, "all", "all", "hostname", "asc")
    return templates.TemplateResponse(
        "hosts.html",
        {
            "request": request,
            "active_page": "hosts",
            "hosts": hosts,
            "collectors": get_collector_statuses(db, settings),
            "current": {
                "q": "",
                "platform": "all",
                "status": "all",
                "sort": "hostname",
                "order": "asc",
            },
        },
    )


@router.get("/shared", response_class=HTMLResponse)
def shared_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Devices monitored by more than one instance (by shared IP)."""
    return templates.TemplateResponse(
        "shared.html",
        {
            "request": request,
            "active_page": "shared",
            "groups": _shared_host_groups(db),
            "collectors": get_collector_statuses(db, settings),
        },
    )


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Active alerts table page."""
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "active_page": "alerts",
            "alerts": _active_alerts(db),
            "collectors": get_collector_statuses(db, settings),
        },
    )


# --- HTMX partials ----------------------------------------------------------


@router.get("/partials/overview", response_class=HTMLResponse)
def overview_partial(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """KPI cards fragment (polled every 60s)."""
    ctx = _overview_context(db, settings)
    ctx["request"] = request
    return templates.TemplateResponse("partials/overview_cards.html", ctx)


@router.get("/partials/hosts", response_class=HTMLResponse)
def hosts_partial(
    request: Request,
    q: str | None = None,
    platform: str = "all",
    status: str = "all",
    sort: str = "hostname",
    order: str = "asc",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Hosts table fragment (live search / filter / sort)."""
    hosts = _hosts_query(db, q, platform, status, sort, order)
    return templates.TemplateResponse(
        "partials/hosts_table.html",
        {
            "request": request,
            "hosts": hosts,
            "current": {
                "q": q or "",
                "platform": platform,
                "status": status,
                "sort": sort,
                "order": order,
            },
        },
    )


@router.get("/partials/alerts", response_class=HTMLResponse)
def alerts_partial(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Alerts table fragment (polled every 60s)."""
    return templates.TemplateResponse(
        "partials/alerts_table.html",
        {"request": request, "alerts": _active_alerts(db)},
    )


@router.get("/partials/health", response_class=HTMLResponse)
def health_partial(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Collector health strip fragment."""
    return templates.TemplateResponse(
        "partials/health_strip.html",
        {"request": request, "collectors": get_collector_statuses(db, settings)},
    )
