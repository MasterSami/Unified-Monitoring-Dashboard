"""HTML pages (Jinja2) and HTMX partial fragments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, distinct, func, select
from sqlalchemy.orm import Session, defer

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
# See Settings.template_auto_reload — this saves a stat() per template per
# include on every render, and the partials re-render on a 60s timer.
templates.env.auto_reload = get_settings().template_auto_reload


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
# Feature flag for the Runbook (admin script library).
templates.env.globals["enable_runbook"] = get_settings().enable_runbook


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


def _count_of(db: Session, stmt: Select) -> int:
    """COUNT the rows a filtered SELECT would return, without projecting them.

    Wrapping the entity SELECT in a subquery makes the database materialize
    every column — including the large JSON payloads — just to count. Swapping
    the column list for ``count()`` lets it answer from an index instead.

    ``maintain_column_froms`` is required: without it SQLAlchemy rebuilds the
    FROM list from the new column list, and ``count()`` names no table — an
    unfiltered statement would then emit a bare ``SELECT count(*)`` and answer 1.
    """
    counted = stmt.with_only_columns(func.count(), maintain_column_froms=True)
    return db.scalar(counted.order_by(None)) or 0


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


def _host_rollup(db: Session) -> list[tuple]:
    """``(instance, platform, status, agent_deployed, count)`` in ONE query.

    The Overview needs the same numbers sliced several ways — per instance, per
    platform, per status, and in total. They all come out of this single GROUP
    BY, so the page no longer issues five separate aggregate queries (and three
    full table scans) on every 60s refresh.
    """
    return db.execute(
        select(
            Host.source_instance,
            Host.source_platform,
            Host.status,
            Host.agent_deployed,
            func.count(Host.id),
        ).group_by(
            Host.source_instance,
            Host.source_platform,
            Host.status,
            Host.agent_deployed,
        )
    ).all()


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


#: Identity of a physical device across tools: its IP, or its name when the tool
#: recorded no IP. Used to count the estate once rather than once per tool.
def _device_key():
    return func.coalesce(func.nullif(Host.ip, ""), func.lower(Host.hostname))


def _distinct_active_hosts(db: Session) -> int:
    """How many DISTINCT devices are currently up, counted once across tools.

    A server watched by both Zabbix and Dynatrace is one server. Summing the
    per-tool totals double-counts every one of them, which is what made the
    headline figure so much larger than the estate actually is.
    """
    return db.scalar(
        select(func.count(distinct(_device_key()))).where(Host.status == HostStatus.up)
    ) or 0


def _critical_alert_count(db: Session, settings: Settings) -> int:
    """Active alerts each tool itself calls most severe (Disaster / Critical).

    Counting the unified 1-5 scale pulled in every platform's top tier at once,
    which produced a number nobody could act on. Matching each tool's own label
    keeps the tile to what a person would recognise as critical.
    """
    labels = settings.critical_alert_label_set
    stmt = select(func.count(Alert.id)).where(Alert.resolved.is_(False))
    if labels:
        stmt = stmt.where(func.lower(Alert.severity_label).in_(labels))
    else:  # no labels configured -> fall back to the unified top severity
        stmt = stmt.where(Alert.severity_int == 5)
    return db.scalar(stmt) or 0


def _native_severity_breakdown(db: Session) -> list[dict]:
    """Active alerts grouped by platform and by each tool's OWN severity label.

    The unified 1-5 scale is right for sorting one mixed list, but it hides what
    each tool actually said: 'Disaster', 'Availability' and 'Major' all collapse
    into the same bucket. This returns them as the tools name them, one group
    per platform, most severe first.
    """
    rows = db.execute(
        select(
            Alert.source_platform,
            Alert.severity_label,
            Alert.severity_int,
            func.count(Alert.id),
        )
        .where(Alert.resolved.is_(False))
        .group_by(Alert.source_platform, Alert.severity_label, Alert.severity_int)
    ).all()

    grouped: dict[str, dict] = {}
    for platform, label, sev, count in rows:
        key = _enum_value(platform)
        entry = grouped.setdefault(key, {"platform": key, "total": 0, "levels": []})
        entry["total"] += int(count)
        entry["levels"].append(
            {
                "label": label or severity_label(int(sev or 1)),
                "severity_int": int(sev or 1),
                "count": int(count),
            }
        )
    for entry in grouped.values():
        entry["levels"].sort(key=lambda lv: (-lv["severity_int"], lv["label"]))
    return sorted(grouped.values(), key=lambda e: -e["total"])


def _per_platform(rows: list[tuple]) -> list[PlatformHostCount]:
    """Hosts per platform (total + down), rolled up from :func:`_host_rollup`.

    Dynatrace is counted differently on purpose: its entity API returns every
    host it has *discovered*, including ones with no OneAgent on them, so a raw
    count does not match the figure on Dynatrace's own deployment page. Only
    hosts with an agent are counted here. Every other platform reports what it
    monitors, so their totals are unchanged.
    """
    totals: dict[str, int] = {}
    downs: dict[str, int] = {}
    discovered: dict[str, int] = {}
    for _instance, platform, status, agent, count in rows:
        key = _enum_value(platform)
        n = int(count)
        discovered[key] = discovered.get(key, 0) + n
        if key == "dynatrace" and agent is not True:
            continue  # discovered, but no OneAgent — not a monitored host
        totals[key] = totals.get(key, 0) + n
        if _enum_value(status) == HostStatus.down.value:
            downs[key] = downs.get(key, 0) + n
    return [
        PlatformHostCount(
            platform=p,
            total=totals.get(p, 0),
            down=downs.get(p, 0),
            discovered=discovered.get(p, 0),
        )
        for p in ("zabbix", "dynatrace", "nnmi", "sitescope")
    ]


def _agent_stats(rows: list[tuple]) -> list[dict]:
    """Per-instance monitoring-agent health, derived from host status.

    Each host's status reflects whether its monitoring agent is reporting
    (Zabbix agent / Dynatrace OneAgent / NNMi SNMP). A "problem" agent is one
    that is down or unknown (disabled hosts are excluded — intentionally off).
    Returns one dict per instance, most-problematic first.
    """
    agg: dict[str, dict] = {}
    for instance, platform, status, _agent, count in rows:
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
        .options(defer(Alert.raw_payload))
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


def _host_status_counts(rows: list[tuple]) -> dict[str, int]:
    """Host counts keyed by normalized status, from :func:`_host_rollup`."""
    counts: dict[str, int] = {}
    for _instance, _platform, status, _agent, count in rows:
        key = _enum_value(status)
        counts[key] = counts.get(key, 0) + int(count)
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

    # Only the five columns the shared table renders — these rows exist purely
    # to be grouped and displayed, so loading whole Host entities (payload JSON
    # included) for every instance sharing each IP was pure waste.
    hosts = list(
        db.scalars(
            select(Host)
            .options(defer(Host.raw_payload), defer(Host.metrics))
            .where(Host.ip.in_(ips))
        ).all()
    )
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
        .options(defer(Alert.raw_payload))
        .where(Alert.resolved.is_(False))
        .order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def _overview_context(db: Session, settings: Settings) -> dict:
    # One host rollup and one alert rollup feed every tile on this page. The
    # totals below used to be five more aggregate queries; they are all just
    # sums of what these two already returned.
    rows = _host_rollup(db)
    buckets = _severity_buckets(db)

    status_counts = _host_status_counts(rows)
    per_platform = _per_platform(rows)
    agent_stats = _agent_stats(rows)
    max_total = max((p.total for p in per_platform), default=0) or 1
    total_hosts = sum(status_counts.values())
    active_alerts = sum(b.count for b in buckets)
    return {
        "total_hosts": total_hosts,
        # The headline tile: distinct devices that are up, counted once even
        # when several tools watch the same server.
        "active_hosts": _distinct_active_hosts(db),
        "status_counts": status_counts,
        "hosts_up": status_counts["up"],
        "hosts_down": status_counts["down"],
        "active_alerts": active_alerts,
        "critical_alerts": _critical_alert_count(db, settings),
        "critical_labels": sorted(settings.critical_alert_label_set),
        "severity_by_platform": _native_severity_breakdown(db),
        "show_host_status_row": settings.show_host_status_row,
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
    """Build the filtered (unordered, unlimited) host SELECT.

    ``raw_payload`` holds the entire source record (for Zabbix, the full
    host.get response including interfaces, tags and inventory) and nothing
    downstream reads it — not a template, not a schema, not an export. Deferring
    it saves loading and JSON-decoding it 300 times per page render.
    """
    stmt = select(Host).options(defer(Host.raw_payload))
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
    total = _count_of(db, stmt)
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
    state: str = "active",
):
    """Alerts SELECT with search + optional started_at range (all in SQL).

    ``state`` selects the lifecycle bucket: ``active`` (unresolved, the default),
    ``resolved`` (history), or ``all`` (both — useful for historical exports).

    ``raw_payload`` is deferred for the same reason as on hosts: it is written
    by the collectors and read by nothing.
    """
    stmt = select(Alert).options(defer(Alert.raw_payload))
    if state == "active":
        stmt = stmt.where(Alert.resolved.is_(False))
    elif state == "resolved":
        stmt = stmt.where(Alert.resolved.is_(True))
    # state == "all" -> no lifecycle filter
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
    state: str = "active",
) -> tuple[list[Alert], int, int, int]:
    """Return (rows, total, page, pages) — one page of alerts for ``state``."""
    stmt = _alerts_filtered_stmt(q, date_from, date_to, state)
    total = _count_of(db, stmt)
    page, pages, offset = _paginate(page, total)
    stmt = (
        stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    rows = list(db.scalars(stmt).all())
    _attach_escalation(db, rows)
    return rows, total, page, pages


def _attach_escalation(db: Session, alerts: list[Alert]) -> None:
    """Annotate each alert with its host owner + a ready-to-send mail link.

    Sets transient (non-persisted) attributes used by the Alerts template:
    ``owner``, ``owner_email`` and ``escalate_href`` (a ``mailto:`` that opens
    Outlook with a pre-filled FYA mail to the owner). A link is produced only
    when the alert is ACTIVE, comes from Zabbix/Dynatrace (NNMi has no host
    owners), and an owner was resolved for its host.
    """
    from urllib.parse import quote

    #: Platforms whose alerts can be escalated to a server owner.
    escalatable = {SourcePlatform.zabbix, SourcePlatform.dynatrace}

    for a in alerts:  # safe defaults so the template never hits an Undefined
        a.owner = None
        a.owner_email = None
        a.escalate_href = None

    # Resolve owners by the HOST external id first (reliable — Dynatrace problems
    # name a service, not the host), then by hostname as a fallback.
    by_ext_id: dict[str, tuple[str | None, str | None]] = {}
    ext_ids = {a.host_external_id for a in alerts if a.host_external_id}
    if ext_ids:
        for ext, owner, email in db.execute(
            select(Host.external_id, Host.owner, Host.owner_email).where(
                Host.external_id.in_(ext_ids)
            )
        ):
            if ext not in by_ext_id or (owner or email):
                by_ext_id[ext] = (owner, email)

    by_name: dict[str, tuple[str | None, str | None]] = {}

    def _record(hostname: str | None, owner, email) -> None:
        key = (hostname or "").strip().lower()
        if key and (key not in by_name or (owner or email)):
            by_name[key] = (owner, email)

    owner_cols = select(Host.hostname, Host.owner, Host.owner_email)
    exact = {a.host_hostname.strip() for a in alerts if a.host_hostname}
    if exact:
        # Match on the raw column so ix_hosts_hostname is usable. Wrapping it in
        # lower() disqualifies the index and turns this into a full scan of
        # hosts — on a page that re-renders every 60s per open tab.
        for hn, owner, email in db.execute(
            owner_cols.where(Host.hostname.in_(exact))
        ):
            _record(hn, owner, email)

    # Only if some alert's hostname didn't match exactly do we pay for the
    # case-insensitive scan, and then only for the names still unresolved.
    missing = {n.lower() for n in exact} - set(by_name)
    if missing:
        for hn, owner, email in db.execute(
            owner_cols.where(func.lower(Host.hostname).in_(missing))
        ):
            _record(hn, owner, email)

    settings = get_settings()
    for a in alerts:
        owner = email = None
        if a.host_external_id and a.host_external_id in by_ext_id:
            owner, email = by_ext_id[a.host_external_id]
        if not (owner or email) and a.host_hostname:
            owner, email = by_name.get(a.host_hostname.strip().lower(), (None, None))
        a.owner, a.owner_email = owner, email
        # Only active Zabbix/Dynatrace alerts with a resolved owner get a link.
        if (
            not a.resolved
            and a.source_platform in escalatable
            and (a.owner or a.owner_email)
        ):
            a.escalate_href = _escalate_mailto(a, quote, settings)


def _escalate_mailto(alert: Alert, quote, settings: Settings) -> str:
    """Build a ``mailto:`` that drafts an FYA escalation to the host owner."""
    host = alert.host_hostname or "the affected server"
    platform = getattr(alert.source_platform, "value", str(alert.source_platform))
    started = (
        alert.started_at.strftime("%Y-%m-%d %H:%M UTC") if alert.started_at else "—"
    )
    subject = f"[Escalation] {alert.title} — {host}"
    owner_line = f"Dears ({alert.owner})," if alert.owner else "Dears,"
    body = (
        f"{owner_line}\n\n"
        "FYA — the following alert is currently active and requires your "
        "attention:\n\n"
        f"Server:   {host}\n"
        f"Problem:  {alert.title}\n"
        f"Severity: {alert.severity_label}\n"
        f"Started:  {started}\n"
        f"Source:   {platform}"
        f"{f' ({alert.source_instance})' if alert.source_instance else ''}\n\n"
        "Kindly investigate and resolve at your earliest convenience.\n\n"
        "Best regards,"
    )
    params = f"subject={quote(subject)}&body={quote(body)}"
    cc = getattr(settings, "escalation_cc", "") or ""
    if cc:
        params += f"&cc={quote(cc)}"
    return f"mailto:{quote(alert.owner_email or '')}?{params}"


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
    return templates.TemplateResponse(request, "overview.html", ctx)


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


# --- Favicon ----------------------------------------------------------------

#: name -> (mtime, svg) — regenerated when the logo file changes.
_FAVICON_CACHE: dict[str, tuple[float, str]] = {}

_FAVICON_FALLBACK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#ffffff"/>'
    '<text x="32" y="44" font-size="34" text-anchor="middle">📊</text></svg>'
)


@router.get("/favicon.svg", include_in_schema=False)
def favicon_svg() -> Response:
    """Browser-tab icon: the logo on a white rounded card.

    Dark browser tab bars swallow logos with dark/transparent backgrounds, so
    the raw file is wrapped in a white rounded rect. The logo is inlined as a
    data URI (external references don't load in SVG favicons) and picked up
    from app/static/logo.svg or logo.png — drop a new file there and the icon
    updates on its own; no code change needed.
    """
    import base64

    static = Path(__file__).resolve().parent.parent / "static"
    for name, mime in (("logo.svg", "image/svg+xml"), ("logo.png", "image/png")):
        path = static / name
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        cached = _FAVICON_CACHE.get(name)
        if cached is None or cached[0] != mtime:
            b64 = base64.b64encode(path.read_bytes()).decode()
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect width="64" height="64" rx="14" fill="#ffffff"/>'
                f'<image x="5" y="5" width="54" height="54" '
                f'href="data:{mime};base64,{b64}" '
                'preserveAspectRatio="xMidYMid meet"/></svg>'
            )
            _FAVICON_CACHE[name] = (mtime, svg)
        return Response(
            _FAVICON_CACHE[name][1],
            media_type="image/svg+xml",
            headers={"Cache-Control": "max-age=300"},
        )
    return Response(
        _FAVICON_FALLBACK,
        media_type="image/svg+xml",
        headers={"Cache-Control": "max-age=300"},
    )


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
        request,
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
    """Capacity table fragment (search / filter / group / sort / paginate).

    The date range is intentionally NOT applied to the live table: capacity is a
    current snapshot, and filtering hosts by ``last_seen`` would silently hide
    every server the moment the window didn't line up (which made the table
    "vanish" when switching platform tabs). The range drives the Excel export's
    trend window only — see ``exportCapacity()`` and ``/api/v1/capacity_report``.
    """
    hosts, total, page, pages = _hosts_query(
        db, q, platform, status, sort, order, instance, page, group
    )
    return templates.TemplateResponse(
        request,
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
    return templates.TemplateResponse(request, "partials/capacity_zabbix_detail.html", ctx)


@router.get("/shared", response_class=HTMLResponse)
def shared_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Devices monitored by more than one instance (by shared IP)."""
    groups, total, page, pages = _shared_host_groups(db, 1)
    return templates.TemplateResponse(
        request,
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
        request,
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
        request,
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
        request,
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
        request,
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
    # Table first, deliberately. The graph renders every node and edge through
    # Cytoscape's force layout, which pins a CPU core for as long as it takes to
    # settle — on a large NNMi map that hangs a laptop. The table shows the same
    # data instantly; the graph is one click away for anyone who wants it.
    mode: str = "table",
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
            request,
            "topology.html",
            {"request": request, "active_page": "topology", "enabled": False},
        )

    if view not in _TOPO_VIEWS:
        view = "network"
    if mode not in ("graph", "table"):
        mode = "table"
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

    return templates.TemplateResponse(request, "topology.html", ctx)


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Alerts table page (defaults to the active/unresolved bucket)."""
    alerts, total, page, pages = _active_alerts(db, None, 1, state="active")
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "request": request,
            "active_page": "alerts",
            "alerts": alerts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "current": {"date_from": "", "date_to": "", "state": "active"},
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
    return templates.TemplateResponse(request, "partials/overview_cards.html", ctx)


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
        request,
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
    state: str = "active",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Alerts table fragment (search + state + date range + paginate)."""
    df, dt = parse_dt(date_from), parse_dt(date_to)
    alerts, total, page, pages = _active_alerts(db, q, page, df, dt, state)
    return templates.TemplateResponse(
        request,
        "partials/alerts_table.html",
        {
            "request": request,
            "alerts": alerts,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "current": {
                "date_from": date_from or "",
                "date_to": date_to or "",
                "state": state,
            },
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
        request,
        "partials/health_strip.html",
        {"request": request, "collectors": get_collector_statuses(db, settings)},
    )
