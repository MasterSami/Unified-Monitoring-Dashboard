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
from app.scheduler import get_collector_statuses, get_service
from app.schemas import PlatformHostCount, SeverityBucket
from app.servers import load_servers
from app.topology import (
    dynatrace_app_rows,
    dynatrace_service_rows,
    dynatrace_unified_rows,
    load_topology,
    nnmi_connection_rows,
)

router = APIRouter(tags=["pages"])

#: Rows per page in the hosts / alerts tables (from config).
PAGE_SIZE = get_settings().page_size


def _paginate(page: int | None, total: int) -> tuple[int, int, int]:
    """Return (page, pages, offset) clamped to a valid range."""
    pages = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = max(1, min(page or 1, pages))
    return page, pages, (page - 1) * PAGE_SIZE


def parse_dt(value: str | None) -> datetime | None:
    """Parse an HTML datetime-local / ISO string into UTC-aware datetime.

    Accepts ``YYYY-MM-DDTHH:MM`` (and with seconds). Returns None for empty or
    unparseable input so callers can treat "no bound" uniformly.
    """
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

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
# Feature flag for the Topology export buttons (NNMi CSV / Dynatrace XLSX).
templates.env.globals["enable_topology_export"] = get_settings().enable_topology_export


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
        for p in ("zabbix", "dynatrace", "nnmi", "sitescope")
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


def _shared_ranked_stmt():
    """Shared IPs ranked by share count desc, hostname asc — all in SQL.

    ``cnt`` = number of distinct instances the device is monitored by. Ordered so
    the most-shared devices come first (tie-break alphabetically). Paginated with
    LIMIT/OFFSET so only one page of IPs is ever materialized.
    """
    cnt = func.count(distinct(Host.source_instance))
    return (
        select(Host.ip.label("ip"), cnt.label("cnt"))
        .where(Host.ip.isnot(None), Host.ip != "")
        .group_by(Host.ip)
        .having(cnt > 1)
        .order_by(cnt.desc(), func.min(Host.hostname).asc())
    )


def _shared_host_groups(
    db: Session, page: int = 1
) -> tuple[list[dict], int, int, int]:
    """Return (groups, total, page, pages) for the shared-devices page.

    Ranking and pagination happen entirely in SQL; only the current page's hosts
    are loaded into Python. Group order follows the SQL rank (share count desc,
    hostname asc).
    """
    total = _shared_hosts_count(db)
    page, pages, offset = _paginate(page, total)

    ranked = db.execute(
        _shared_ranked_stmt().limit(PAGE_SIZE).offset(offset)
    ).all()
    ips = [r.ip for r in ranked]
    if not ips:
        return [], total, page, pages

    hosts = list(db.scalars(select(Host).where(Host.ip.in_(ips))).all())
    by_ip: dict[str, list[Host]] = {}
    for h in hosts:
        by_ip.setdefault(str(h.ip), []).append(h)

    groups: list[dict] = []
    for r in ranked:  # preserve the SQL rank order
        members = by_ip.get(str(r.ip), [])
        instances = {m.source_instance for m in members}
        if len(instances) < 2:
            continue
        members = sorted(
            members, key=lambda m: (m.source_platform.value, m.source_instance)
        )
        groups.append(
            {
                "ip": r.ip,
                "hostname": members[0].hostname,
                "members": members,
                "instance_count": len(instances),
                "platforms": sorted({m.source_platform.value for m in members}),
            }
        )
    return groups, total, page, pages


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
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """Build the filtered (unordered, unlimited) host SELECT."""
    stmt = select(Host)
    if platform and platform != "all":
        stmt = stmt.where(Host.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(Host.source_instance == instance)
    if group and group != "all":
        stmt = stmt.where(Host.group_name == group)
    if date_from is not None:
        stmt = stmt.where(Host.last_seen >= date_from)
    if date_to is not None:
        stmt = stmt.where(Host.last_seen <= date_to)
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
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[Host], int, int, int]:
    """Return (rows, total, page, pages) — one page of the filtered hosts."""
    stmt = _hosts_stmt(q, platform, status, instance, group, date_from, date_to)
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


def _alerts_filtered_stmt(
    q: str | None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """Active-alerts SELECT with search + optional started_at range (all in SQL)."""
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
    if date_from is not None:
        stmt = stmt.where(Alert.started_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Alert.started_at <= date_to)
    return stmt


def _active_alerts(
    db: Session,
    q: str | None = None,
    page: int | None = 1,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[Alert], int, int, int]:
    """Return (rows, total, page, pages) — one page of active alerts."""
    stmt = _alerts_filtered_stmt(q, date_from, date_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page, pages, offset = _paginate(page, total)
    stmt = (
        stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    return list(db.scalars(stmt).all()), total, page, pages


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


def _instance_names(settings: Settings, db: Session | None = None) -> list[dict]:
    """Instances (name + platform) for filter dropdowns.

    Configured pull instances (servers.yaml) plus push instances that only exist
    in the data (e.g. SiteScope forwarders), so SiteScope shows up in the filter
    even though it isn't in the server inventory.
    """
    out = [{"name": s.name, "platform": s.platform} for s in load_servers(settings)]
    if db is not None:
        seen = {i["name"] for i in out}
        rows = db.execute(
            select(Host.source_instance, Host.source_platform)
            .where(Host.source_platform == SourcePlatform.sitescope)
            .group_by(Host.source_instance, Host.source_platform)
            .order_by(Host.source_instance)
        ).all()
        for name, platform in rows:
            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "platform": getattr(platform, "value", str(platform))})
    return out


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
            "instances": _instance_names(settings, db),
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
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Capacity table fragment (search / filter / group / sort / date / paginate)."""
    df, dt = parse_dt(date_from), parse_dt(date_to)
    hosts, total, page, pages = _hosts_query(
        db, q, platform, status, sort, order, instance, page, group, df, dt
    )
    return templates.TemplateResponse(
        "partials/capacity_table.html",
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
                "group": group,
                "sort": sort,
                "order": order,
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
        },
    )


@router.get("/partials/capacity/zabbix-detail", response_class=HTMLResponse)
def capacity_zabbix_detail_partial(
    request: Request,
    instance: str,
    hostid: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Lazily-loaded Zabbix capacity detail for one host (cached per interval).

    Loaded via HTMX only when a Zabbix server is opened, so the detail is never
    computed for rows the user never clicks.
    """
    ctx: dict = {"request": request, "instance": instance, "detail": None, "error": None}
    collector = get_service().get(instance)
    if collector is None or getattr(collector, "name", "") != "zabbix":
        ctx["error"] = f"No Zabbix collector configured for '{instance}'."
    elif settings.mock_mode:
        ctx["error"] = "Detailed report needs a live Zabbix connection (MOCK_MODE is on)."
    else:
        try:
            from app.zabbix_report import host_capacity_detail

            ctx["detail"] = host_capacity_detail(collector, hostid)
        except Exception as exc:  # noqa: BLE001 — surface, never 500 the panel
            ctx["error"] = f"Could not load detail: {exc}"
    return templates.TemplateResponse("partials/capacity_zabbix_detail.html", ctx)


@router.get("/shared", response_class=HTMLResponse)
def shared_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Devices monitored by more than one instance (by shared IP)."""
    groups, total, page, pages = _shared_host_groups(db, 1)
    return templates.TemplateResponse(
        "shared.html",
        {
            "request": request,
            "active_page": "shared",
            "groups": groups,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "collectors": get_collector_statuses(db, settings),
        },
    )


@router.get("/partials/shared", response_class=HTMLResponse)
def shared_partial(
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Shared-devices list fragment (server-side paginated)."""
    groups, total, page, pages = _shared_host_groups(db, page)
    return templates.TemplateResponse(
        "partials/shared_table.html",
        {
            "request": request,
            "groups": groups,
            "total": total,
            "page": page,
            "pages": pages,
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
            "instances": _instance_names(settings, db),
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
        "edge_word": "L2 connections",
    },
    "service": {
        "platform": SourcePlatform.dynatrace,
        "label": "Service map",
        "source": "Dynatrace",
        "node_word": "Services",
        "edge_word": "Dependencies",
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
    sub: str = "unified",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Topology views: NNMi network map + Dynatrace unified service map.

    Two view types (network / service), each viewable as a **table** or an
    interactive **graph**. The service table has sub-views matching the export
    sheets (unified / app→app / service→service). Gated behind
    ``ENABLE_TOPOLOGY`` (default off) so the user turns it on themselves.
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
    if sub not in ("unified", "app", "service"):
        sub = "unified"
    meta = _TOPO_VIEWS[view]
    platform = meta["platform"]

    instances = _topology_instances(db, platform)
    names = [i["name"] for i in instances]
    if instance not in names:
        instance = names[0] if names else None

    nodes: list[TopologyNode] = []
    edges: list[TopologyEdge] = []
    if instance:
        nodes, edges = load_topology(db, platform, instance)

    ctx = {
        "request": request,
        "active_page": "topology",
        "enabled": True,
        "view": view,
        "mode": mode,
        "sub": sub,
        "meta": meta,
        "views": _TOPO_VIEWS,
        "instance": instance,
        "instances": instances,
        "nodes": nodes,
        "node_count": len(nodes),
    }

    if view == "network":
        connections = nnmi_connection_rows(edges)
        ctx.update({"connections": connections, "edge_count": len(connections)})
    else:
        unified = dynatrace_unified_rows(edges)
        ctx.update(
            {
                "unified": unified,
                "app2app": dynatrace_app_rows(unified),
                "svc2svc": dynatrace_service_rows(unified),
                "edge_count": len(unified),
            }
        )

    return templates.TemplateResponse("topology.html", ctx)


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
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Alerts table fragment (search + date range + paginate + polled every 60s)."""
    df, dt = parse_dt(date_from), parse_dt(date_to)
    alerts, total, page, pages = _active_alerts(db, q, page, df, dt)
    return templates.TemplateResponse(
        "partials/alerts_table.html",
        {
            "request": request,
            "alerts": alerts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "current": {"date_from": date_from or "", "date_to": date_to or ""},
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
