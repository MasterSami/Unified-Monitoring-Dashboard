"""HTML pages (Jinja2) and HTMX partial fragments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    Alert,
    Host,
    HostStatus,
    SourcePlatform,
    TopologyEdge,
    TopologyNode,
)
from app.normalizer import severity_label
from app.scheduler import get_collector_statuses
from app.schemas import PlatformHostCount, SeverityBucket
from app.servers import load_servers

router = APIRouter(tags=["pages"])

#: Rows per page in the hosts / alerts tables (from config).
PAGE_SIZE = get_settings().page_size


def _paginate(page: int | None, total: int) -> tuple[int, int, int]:
    """Return (page, pages, offset) clamped to a valid range."""
    pages = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = max(1, min(page or 1, pages))
    return page, pages, (page - 1) * PAGE_SIZE

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = _TEMPLATE_DIR.parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _asset_version() -> str:
    """A cache-busting token derived from the newest static asset mtime.

    Appended to CSS/JS URLs so browsers refetch them after a redeploy instead of
    serving a stale cached copy.
    """
    try:
        newest = max(
            p.stat().st_mtime
            for p in _STATIC_DIR.glob("*")
            if p.is_file()
        )
        return str(int(newest))
    except (OSError, ValueError):
        return "1"


# Exposed to every template as {{ asset_version }}.
templates.env.globals["asset_version"] = _asset_version()
# Feature flag for the (in-progress) Topology / Service-Map / App-Map views.
templates.env.globals["enable_topology"] = get_settings().enable_topology
# Feature flag for the CSV export buttons.
templates.env.globals["enable_export"] = get_settings().enable_export


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


def _agent_stats(db: Session) -> list[dict]:
    """Per-instance monitoring-agent health, derived from host status.

    Each host's status reflects whether its monitoring agent is reporting
    (Zabbix agent / Dynatrace OneAgent / NNMi SNMP). A "problem" agent is one
    that is down or unknown (disabled hosts are excluded — intentionally off).
    Returns one dict per instance, most-problematic first.
    """
    rows = db.execute(
        select(
            Host.source_instance,
            Host.source_platform,
            Host.status,
            func.count(Host.id),
        ).group_by(Host.source_instance, Host.source_platform, Host.status)
    ).all()

    agg: dict[str, dict] = {}
    for instance, platform, status, count in rows:
        entry = agg.setdefault(
            instance or "—",
            {
                "instance": instance or "—",
                "platform": getattr(platform, "value", str(platform)),
                "up": 0,
                "down": 0,
                "unknown": 0,
                "disabled": 0,
            },
        )
        key = status.value if hasattr(status, "value") else str(status)
        entry[key] = entry.get(key, 0) + int(count)

    out: list[dict] = []
    for entry in agg.values():
        entry["total"] = (
            entry["up"] + entry["down"] + entry["unknown"] + entry["disabled"]
        )
        entry["problems"] = entry["down"] + entry["unknown"]
        entry["has_problem"] = entry["problems"] > 0
        out.append(entry)
    out.sort(key=lambda e: (-e["problems"], e["instance"]))
    return out


def _annotate_agent_alerts(db: Session, hosts: list[Host]) -> None:
    """Attach ``alert_count`` and ``max_sev`` (active alerts) to each host.

    Alerts are matched to a host by (source_instance, host_hostname). Only the
    hosts on the current page are looked up, so this stays cheap.
    """
    for h in hosts:
        h.alert_count = 0  # type: ignore[attr-defined]
        h.max_sev = 0  # type: ignore[attr-defined]
    if not hosts:
        return
    hostnames = list({h.hostname for h in hosts})
    rows = db.execute(
        select(
            Alert.source_instance,
            Alert.host_hostname,
            func.count(Alert.id),
            func.max(Alert.severity_int),
        )
        .where(Alert.resolved.is_(False), Alert.host_hostname.in_(hostnames))
        .group_by(Alert.source_instance, Alert.host_hostname)
    ).all()
    counts = {
        (inst, hn): (int(cnt), int(sev or 0)) for inst, hn, cnt, sev in rows
    }
    for h in hosts:
        cnt, sev = counts.get((h.source_instance, h.hostname), (0, 0))
        h.alert_count = cnt  # type: ignore[attr-defined]
        h.max_sev = sev  # type: ignore[attr-defined]


def _agent_alerts(db: Session, instance: str, hostname: str) -> list[Alert]:
    """Active alerts for one agent (host), most severe first."""
    stmt = (
        select(Alert)
        .where(
            Alert.resolved.is_(False),
            Alert.source_instance == instance,
            Alert.host_hostname == hostname,
        )
        .order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
    )
    return list(db.scalars(stmt).all())


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


def _shared_ips_stmt():
    """SELECT of IPs monitored by 2+ distinct instances (device overlap)."""
    return (
        select(Host.ip)
        .where(Host.ip.isnot(None), Host.ip != "")
        .group_by(Host.ip)
        .having(func.count(distinct(Host.source_instance)) > 1)
    )


def _shared_hosts_count(db: Session) -> int:
    """Number of devices monitored by more than one instance (SQL, cheap)."""
    return db.scalar(select(func.count()).select_from(_shared_ips_stmt().subquery())) or 0


def _shared_host_groups(db: Session, limit: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """Return (groups, total). Groups the shared-IP devices; loads only those hosts.

    The same physical device monitored by more than one instance shows up as
    several :class:`Host` rows with the same IP. Only IPs with 2+ distinct
    instances are loaded, so this stays cheap on large environments.
    """
    total = _shared_hosts_count(db)
    shared_ips = list(db.scalars(_shared_ips_stmt().limit(limit)).all())
    if not shared_ips:
        return [], total

    hosts = list(db.scalars(select(Host).where(Host.ip.in_(shared_ips))).all())
    by_ip: dict[str, list[Host]] = {}
    for h in hosts:
        by_ip.setdefault(str(h.ip), []).append(h)

    groups: list[dict] = []
    for ip, members in by_ip.items():
        instances = {m.source_instance for m in members}
        if len(instances) < 2:
            continue
        members = sorted(
            members, key=lambda m: (m.source_platform.value, m.source_instance)
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
    return groups, total


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
    critical_alerts = (
        db.scalar(
            select(func.count(Alert.id)).where(
                Alert.resolved.is_(False), Alert.severity_int == 5
            )
        )
        or 0
    )
    buckets = _severity_buckets(db)
    per_platform = _per_platform(db)
    max_total = max((p.total for p in per_platform), default=0) or 1
    status_counts = _host_status_counts(db)
    agent_stats = _agent_stats(db)
    return {
        "total_hosts": total_hosts,
        "status_counts": status_counts,
        "hosts_up": status_counts["up"],
        "hosts_down": status_counts["down"],
        "active_alerts": active_alerts,
        "critical_alerts": critical_alerts,
        "per_platform": per_platform,
        "platform_max": max_total,
        "severity_buckets": buckets,
        "donut_gradient": _donut_gradient(buckets),
        "recent_critical": _recent_critical(db),
        "shared_count": _shared_hosts_count(db),
        "agent_stats": agent_stats,
        "agent_problem_total": sum(a["problems"] for a in agent_stats),
        "agent_problem_instances": sum(1 for a in agent_stats if a["has_problem"]),
        "collectors": get_collector_statuses(db, settings),
    }


def _hosts_stmt(
    q: str | None,
    platform: str | None,
    status: str | None,
    instance: str | None,
    group: str | None = "all",
):
    """Build the filtered (unordered, unlimited) host SELECT."""
    stmt = select(Host)
    if platform and platform != "all":
        stmt = stmt.where(Host.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(Host.source_instance == instance)
    if group and group != "all":
        stmt = stmt.where(Host.group_name == group)
    if status == "issues":
        # A problem agent: down or unknown (disabled hosts are intentional).
        stmt = stmt.where(
            Host.status.in_([HostStatus.down, HostStatus.unknown])
        )
    elif status and status != "all":
        stmt = stmt.where(Host.status == status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Host.hostname).like(like)
            | func.lower(func.coalesce(Host.ip, "")).like(like)
            | func.lower(func.coalesce(Host.group_name, "")).like(like)
            | func.lower(func.coalesce(Host.source_instance, "")).like(like)
        )
    return stmt


def _hosts_query(
    db: Session,
    q: str | None,
    platform: str | None,
    status: str | None,
    sort: str,
    order: str,
    instance: str | None = "all",
    page: int | None = 1,
    group: str | None = "all",
) -> tuple[list[Host], int, int, int]:
    """Return (rows, total, page, pages) — one page of the filtered hosts."""
    stmt = _hosts_stmt(q, platform, status, instance, group)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page, pages, offset = _paginate(page, total)
    sort_cols = {
        "hostname": Host.hostname,
        "ip": Host.ip,
        "platform": Host.source_platform,
        "instance": Host.source_instance,
        "status": Host.status,
        "group": Host.group_name,
        "last_seen": Host.last_seen,
        "cpu": Host.cpu_pct,
        "mem": Host.mem_pct,
        "disk": Host.disk_pct,
    }
    col = sort_cols.get(sort, Host.hostname)
    stmt = (
        stmt.order_by(col.desc() if order == "desc" else col.asc())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    return list(db.scalars(stmt).all()), total, page, pages


def _active_alerts(
    db: Session, q: str | None = None, page: int | None = 1
) -> tuple[list[Alert], int, int, int]:
    """Return (rows, total, page, pages) — one page of active alerts."""
    stmt = select(Alert).where(Alert.resolved.is_(False))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Alert.title).like(like)
            | func.lower(func.coalesce(Alert.host_hostname, "")).like(like)
            | func.lower(func.coalesce(Alert.source_instance, "")).like(like)
            | func.lower(Alert.source_platform).like(like)
            | func.lower(Alert.severity_label).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page, pages, offset = _paginate(page, total)
    stmt = (
        stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    return list(db.scalars(stmt).all()), total, page, pages


# --- Capacity helpers -------------------------------------------------------


def _group_names(db: Session) -> list[str]:
    """Distinct host groups (for the Capacity group selector), sorted."""
    rows = db.execute(
        select(distinct(Host.group_name))
        .where(Host.group_name.isnot(None), Host.group_name != "")
        .order_by(Host.group_name)
    ).all()
    return [r[0] for r in rows]


def _capacity_summary(
    db: Session,
    q: str | None,
    platform: str | None,
    status: str | None,
    instance: str | None,
    group: str | None,
) -> dict:
    """Aggregate CPU/mem/disk over the *entire* filtered selection (via SQL).

    Averages and peaks reflect every host that matches the current filters (a
    single server, a group, or all), not just the visible page — so the capacity
    team gets a true rollup. Hosts with no metric are excluded from that metric's
    average.
    """
    base = _hosts_stmt(q, platform, status, instance, group).subquery()
    row = db.execute(
        select(
            func.count(),
            func.avg(base.c.cpu_pct),
            func.max(base.c.cpu_pct),
            func.avg(base.c.mem_pct),
            func.max(base.c.mem_pct),
            func.avg(base.c.disk_pct),
            func.max(base.c.disk_pct),
        )
    ).one()
    count, cpu_avg, cpu_max, mem_avg, mem_max, disk_avg, disk_max = row

    def _mk(avg, mx):
        return {
            "avg": round(float(avg), 1) if avg is not None else None,
            "max": round(float(mx), 1) if mx is not None else None,
        }

    # The single hottest host by CPU in the current selection.
    hottest = db.scalars(
        _hosts_stmt(q, platform, status, instance, group)
        .where(Host.cpu_pct.isnot(None))
        .order_by(Host.cpu_pct.desc())
        .limit(1)
    ).first()

    return {
        "count": int(count or 0),
        "cpu": _mk(cpu_avg, cpu_max),
        "mem": _mk(mem_avg, mem_max),
        "disk": _mk(disk_avg, disk_max),
        "hottest": hottest,
    }


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


def _instance_names(settings: Settings) -> list[dict]:
    """Configured instances (name + platform) for filter dropdowns."""
    return [
        {"name": s.name, "platform": s.platform} for s in load_servers(settings)
    ]


@router.get("/hosts")
def hosts_page() -> RedirectResponse:
    """Legacy path — the Hosts view is now the Capacity view."""
    return RedirectResponse(url="/capacity", status_code=307)


_CAPACITY_CURRENT_DEFAULT = {
    "q": "",
    "platform": "all",
    "instance": "all",
    "status": "all",
    "group": "all",
    "sort": "hostname",
    "order": "asc",
}


@router.get("/capacity", response_class=HTMLResponse)
def capacity_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Capacity view — server resource utilization for the capacity team.

    Filter to a server, instance, or group and read CPU / memory / disk
    utilization plus an aggregate rollup (averages, peaks, hottest host).
    """
    hosts, total, page, pages = _hosts_query(
        db, None, "all", "all", "hostname", "asc", "all", 1, "all"
    )
    summary = _capacity_summary(db, None, "all", "all", "all", "all")
    return templates.TemplateResponse(
        "capacity.html",
        {
            "request": request,
            "active_page": "capacity",
            "hosts": hosts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "instances": _instance_names(settings),
            "groups": _group_names(db),
            "summary": summary,
            "collectors": get_collector_statuses(db, settings),
            "current": dict(_CAPACITY_CURRENT_DEFAULT),
        },
    )


@router.get("/partials/capacity", response_class=HTMLResponse)
def capacity_partial(
    request: Request,
    q: str | None = None,
    platform: str = "all",
    instance: str = "all",
    status: str = "all",
    group: str = "all",
    sort: str = "hostname",
    order: str = "asc",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Capacity table fragment (search / filter / group / sort / paginate)."""
    hosts, total, page, pages = _hosts_query(
        db, q, platform, status, sort, order, instance, page, group
    )
    summary = _capacity_summary(db, q, platform, status, instance, group)
    return templates.TemplateResponse(
        "partials/capacity_table.html",
        {
            "request": request,
            "hosts": hosts,
            "total": total,
            "page": page,
            "pages": pages,
            "summary": summary,
            "current": {
                "q": q or "",
                "platform": platform,
                "instance": instance,
                "status": status,
                "group": group,
                "sort": sort,
                "order": order,
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
    groups, total = _shared_host_groups(db)
    return templates.TemplateResponse(
        "shared.html",
        {
            "request": request,
            "active_page": "shared",
            "groups": groups,
            "total": total,
            "limit": PAGE_SIZE,
            "collectors": get_collector_statuses(db, settings),
        },
    )


@router.get("/agents", response_class=HTMLResponse)
def agents_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Agents (monitored hosts) with their status and the alerts on each."""
    hosts, total, page, pages = _hosts_query(
        db, None, "all", "all", "hostname", "asc", "all", 1
    )
    _annotate_agent_alerts(db, hosts)
    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "active_page": "agents",
            "hosts": hosts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "instances": _instance_names(settings),
            "collectors": get_collector_statuses(db, settings),
            "current": {
                "q": "",
                "platform": "all",
                "instance": "all",
                "status": "all",
                "sort": "hostname",
                "order": "asc",
            },
        },
    )


@router.get("/partials/agents", response_class=HTMLResponse)
def agents_partial(
    request: Request,
    q: str | None = None,
    platform: str = "all",
    instance: str = "all",
    status: str = "all",
    sort: str = "hostname",
    order: str = "asc",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Agents table fragment (search / filter / sort / paginate)."""
    hosts, total, page, pages = _hosts_query(
        db, q, platform, status, sort, order, instance, page
    )
    _annotate_agent_alerts(db, hosts)
    return templates.TemplateResponse(
        "partials/agents_table.html",
        {
            "request": request,
            "hosts": hosts,
            "total": total,
            "page": page,
            "pages": pages,
            "current": {
                "q": q or "",
                "platform": platform,
                "instance": instance,
                "status": status,
                "sort": sort,
                "order": order,
            },
        },
    )


@router.get("/partials/agent-detail", response_class=HTMLResponse)
def agent_detail_partial(
    request: Request,
    instance: str,
    hostname: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Full detail for one agent: its host record + the active alerts on it."""
    host = db.scalar(
        select(Host).where(
            Host.source_instance == instance, Host.hostname == hostname
        )
    )
    return templates.TemplateResponse(
        "partials/agent_detail.html",
        {
            "request": request,
            "host": host,
            "instance": instance,
            "hostname": hostname,
            "alerts": _agent_alerts(db, instance, hostname),
        },
    )


#: UI "view" -> the platform that owns that kind of topology graph.
_TOPO_VIEWS = {
    "network": {
        "platform": SourcePlatform.nnmi,
        "label": "Network topology",
        "source": "NNMi",
        "node_word": "Devices",
        "edge_word": "L2 links",
    },
    "service": {
        "platform": SourcePlatform.dynatrace,
        "label": "Service map",
        "source": "Dynatrace",
        "node_word": "Services",
        "edge_word": "Calls",
    },
}


def _topology_instances(db: Session, platform: SourcePlatform) -> list[dict]:
    """Instances (with node counts) that currently have topology for a platform."""
    rows = db.execute(
        select(
            TopologyNode.source_instance, func.count(TopologyNode.id)
        )
        .where(TopologyNode.source_platform == platform)
        .group_by(TopologyNode.source_instance)
        .order_by(TopologyNode.source_instance)
    ).all()
    return [{"name": inst, "nodes": int(cnt)} for inst, cnt in rows]


@router.get("/topology", response_class=HTMLResponse)
def topology_page(
    request: Request,
    view: str = "network",
    instance: str | None = None,
    mode: str = "graph",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Topology views: NNMi network map + Dynatrace service map.

    Two view types (network / service), each viewable as a **table** or an
    interactive **graph**. Gated behind ``ENABLE_TOPOLOGY`` (default off) so the
    user turns it on themselves.
    """
    if not settings.enable_topology:
        return templates.TemplateResponse(
            "topology.html",
            {"request": request, "active_page": "topology", "enabled": False},
        )

    if view not in _TOPO_VIEWS:
        view = "network"
    if mode not in ("graph", "table"):
        mode = "graph"
    meta = _TOPO_VIEWS[view]
    platform = meta["platform"]

    instances = _topology_instances(db, platform)
    names = [i["name"] for i in instances]
    if instance not in names:
        instance = names[0] if names else None

    nodes: list[TopologyNode] = []
    edges: list[TopologyEdge] = []
    if instance:
        nodes = list(
            db.scalars(
                select(TopologyNode)
                .where(
                    TopologyNode.source_platform == platform,
                    TopologyNode.source_instance == instance,
                )
                .order_by(TopologyNode.name)
            ).all()
        )
        edges = list(
            db.scalars(
                select(TopologyEdge).where(
                    TopologyEdge.source_platform == platform,
                    TopologyEdge.source_instance == instance,
                )
            ).all()
        )
    name_by_ext = {n.external_id: n.name for n in nodes}
    # Only connections whose endpoints are present as nodes (for the table).
    edge_rows = [
        {
            "from": name_by_ext.get(e.from_external_id, e.from_external_id),
            "to": name_by_ext.get(e.to_external_id, e.to_external_id),
            "label": e.label or "",
            "kind": e.kind,
        }
        for e in edges
        if e.from_external_id in name_by_ext and e.to_external_id in name_by_ext
    ]
    edge_rows.sort(key=lambda r: (r["from"], r["to"]))

    return templates.TemplateResponse(
        "topology.html",
        {
            "request": request,
            "active_page": "topology",
            "enabled": True,
            "view": view,
            "mode": mode,
            "meta": meta,
            "views": _TOPO_VIEWS,
            "instance": instance,
            "instances": instances,
            "nodes": nodes,
            "edges": edge_rows,
            "node_count": len(nodes),
            "edge_count": len(edge_rows),
        },
    )


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Active alerts table page."""
    alerts, total, page, pages = _active_alerts(db, None, 1)
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "active_page": "alerts",
            "alerts": alerts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
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
    instance: str = "all",
    status: str = "all",
    sort: str = "hostname",
    order: str = "asc",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Hosts table fragment (live search / filter / sort / paginate)."""
    hosts, total, page, pages = _hosts_query(
        db, q, platform, status, sort, order, instance, page
    )
    return templates.TemplateResponse(
        "partials/hosts_table.html",
        {
            "request": request,
            "hosts": hosts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "current": {
                "q": q or "",
                "platform": platform,
                "instance": instance,
                "status": status,
                "sort": sort,
                "order": order,
            },
        },
    )


@router.get("/partials/alerts", response_class=HTMLResponse)
def alerts_partial(
    request: Request,
    q: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Alerts table fragment (search + paginate + polled every 60s)."""
    alerts, total, page, pages = _active_alerts(db, q, page)
    return templates.TemplateResponse(
        "partials/alerts_table.html",
        {
            "request": request,
            "alerts": alerts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
        },
    )


@router.get("/partials/health", response_class=HTMLResponse)
def health_partial(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Collector health strip fragment (sidebar)."""
    return templates.TemplateResponse(
        "partials/health_strip.html",
        {"request": request, "collectors": get_collector_statuses(db, settings)},
    )
