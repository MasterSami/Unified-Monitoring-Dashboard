"""Role 3 - the tool broker.

Exactly four read-only tools, all answered from SAMIX's own tables in this
same process. Nothing here opens a connection to Zabbix, Dynatrace, NNMi or
SiteScope - SAMIX is already the broker that normalized their data.

Every call goes through :meth:`ToolBroker.execute`, which enforces the
allow-list, validates arguments against the tool's pydantic schema, runs the
tool in a worker thread with its own DB session under a timeout, and caps the
rows returned. Rows always carry ``record_id``, ``source_platform``,
``source_instance`` and a timestamp, because the evidence pack has to cite
where each fact came from.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, defer

from app.config import Settings
from app.db import SessionLocal
from app.models import PLATFORM_ORDER, Alert, CapacityForecast, Host, LIVE_PLATFORMS
from app.sitescope import dedup_key

logger = logging.getLogger("ai.tools")

#: Tool name the model asked for is not one of ours.
class UnknownToolError(ValueError):
    pass


#: Arguments did not fit the tool's schema.
class ToolArgumentError(ValueError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


# --- Hostname resolution ----------------------------------------------------


@dataclass
class Resolution:
    """How an informal name ("MW10") mapped onto known hosts.

    ``status`` is one of exact / fuzzy / none / ambiguous. A resolution never
    guesses: ambiguous returns the candidates so the model can ask the user.
    """

    query: str
    status: str
    hosts: list[Host] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)

    @property
    def keys(self) -> set[str]:
        return {dedup_key(h.hostname) for h in self.hosts if h.hostname}

    @property
    def ips(self) -> set[str]:
        out: set[str] = set()
        for h in self.hosts:
            for ip in (h.ip, *(h.ip_all or "").split(",")):
                if ip and ip.strip():
                    out.add(ip.strip())
        return out


MAX_CANDIDATES = 5


def resolve_hostname(db: Session, name: str) -> Resolution:
    """Resolve the user's name against the Host table.

    Exact match on the normalized hostname (lowercase, domain stripped) wins
    and returns every platform's row for that device. Otherwise a
    case-insensitive *contains* search; one distinct device -> fuzzy, up to
    ``MAX_CANDIDATES`` -> ambiguous (with the list), more -> ambiguous with
    the first five, nothing -> none.
    """
    query = (name or "").strip()
    norm = dedup_key(query)
    if not norm:
        return Resolution(query, "none")

    like = f"%{query}%"
    rows = db.scalars(
        select(Host)
        .options(defer(Host.raw_payload), defer(Host.metrics))
        .where(or_(Host.hostname.ilike(like), Host.ip == query, Host.ip_all.ilike(like)))
        .order_by(Host.hostname)
        .limit(400)
    ).all()
    if not rows:
        return Resolution(query, "none")

    exact = [h for h in rows if dedup_key(h.hostname) == norm or h.ip == query]
    if exact:
        return Resolution(query, "exact", exact)

    by_device: dict[str, list[Host]] = {}
    for h in rows:
        by_device.setdefault(dedup_key(h.hostname) or (h.ip or ""), []).append(h)
    if len(by_device) == 1:
        return Resolution(query, "fuzzy", rows)
    names = sorted(by_device)
    return Resolution(query, "ambiguous", [], names[:MAX_CANDIDATES] + (["..."] if len(names) > MAX_CANDIDATES else []))


def _resolution_rows(res: Resolution) -> list[dict[str, Any]]:
    """The host rows a resolution found, as evidence."""
    return [
        {
            "record_id": f"host:{h.id}",
            "hostname": h.hostname,
            "ip": h.ip,
            "source_platform": h.source_platform.value,
            "source_instance": h.source_instance,
            "status": h.status.value,
            "group": h.group_name,
            "last_seen": _iso(h.last_seen),
            "updated_at": _iso(h.updated_at),
            "monitoring_agent_deployed": h.agent_deployed,
            "live_platform": h.source_platform.value in LIVE_PLATFORMS,
        }
        for h in res.hosts
    ]


def _unresolved(res: Resolution) -> dict[str, Any]:
    """The shape every host tool returns when the name did not resolve."""
    if res.status == "none":
        return {"rows": [], "meta": {"resolution": "none", "query": res.query,
                                     "message": f"No host matching '{res.query}' in SAMIX."}}
    return {"rows": [], "meta": {"resolution": "ambiguous", "query": res.query,
                                 "candidates": res.candidates,
                                 "message": f"'{res.query}' matches several hosts; ask the user which one."}}


# --- Tool inputs -----------------------------------------------------------


class HostAlertsIn(BaseModel):
    hostname: str = Field(min_length=1, max_length=255, description="Hostname as the user wrote it, e.g. MW10")
    active_only: bool = Field(default=True, description="Only alerts still open (default). False includes resolved ones.")


class HostStatusIn(BaseModel):
    hostname: str = Field(min_length=1, max_length=255, description="Hostname as the user wrote it")


class AlertsSummaryIn(BaseModel):
    severity_min: int | None = Field(default=None, ge=1, le=5, description="Minimum severity 1 (info) .. 5 (critical/disaster)")
    service: str | None = Field(default=None, max_length=255, description="Service or host-group name to filter on")
    limit: int = Field(default=20, ge=1, le=50)


class CapacityRiskIn(BaseModel):
    hostname: str | None = Field(default=None, max_length=255, description="Limit to one host (informal name is fine)")
    classification: str | None = Field(default=None, description="critical | warning | watch | ok | noisy | insufficient_data")


# --- Tool implementations ----------------------------------------------------
# Each returns {"rows": [...], "meta": {...}}. Rows are plain dicts.


def _alert_row(a: Alert) -> dict[str, Any]:
    return {
        "record_id": f"alert:{a.id}",
        "source_platform": a.source_platform.value,
        "source_instance": a.source_instance,
        "host": a.host_hostname,
        "host_ip": a.host_ip,
        "severity": a.severity_int,
        "severity_label": a.severity_label,
        "title": a.title,
        "state": "resolved" if a.resolved else "active",
        "started_at": _iso(a.started_at),
        "last_seen": _iso(a.last_seen or a.updated_at),
        "resolved_at": _iso(a.resolved_at),
        "problem_type": a.problem_type,
        "service": a.host_group or a.business_service,
    }


def get_host_alerts(db: Session, args: HostAlertsIn) -> dict[str, Any]:
    res = resolve_hostname(db, args.hostname)
    if res.status in ("none", "ambiguous"):
        return _unresolved(res)
    keys, ips = res.keys, res.ips
    ext = {(h.source_platform, h.source_instance, h.external_id) for h in res.hosts}
    norm = dedup_key(args.hostname)

    conditions = [Alert.host_hostname.ilike(f"%{norm}%")]
    if ips:
        conditions.append(Alert.host_ip.in_(sorted(ips)))
    if ext:
        conditions.append(Alert.host_external_id.in_(sorted({e[2] for e in ext})))
    if keys:
        conditions.append(Alert.dedup_key.in_(sorted(keys)))
    stmt = select(Alert).options(defer(Alert.raw_payload)).where(or_(*conditions))
    if args.active_only:
        stmt = stmt.where(Alert.resolved.is_(False))
    stmt = stmt.order_by(Alert.resolved.asc(), Alert.severity_int.desc(), Alert.started_at.desc().nullslast()).limit(300)

    rows = []
    for a in db.scalars(stmt).all():
        same_host = (
            dedup_key(a.host_hostname) in keys
            or (a.host_ip and a.host_ip in ips)
            or (a.dedup_key and a.dedup_key in keys)
            or (a.host_external_id and (a.source_platform, a.source_instance, a.host_external_id) in ext)
        )
        if same_host:
            rows.append(_alert_row(a))
    platforms_with_alerts = sorted({r["source_platform"] for r in rows})
    monitored_by = sorted({h.source_platform.value for h in res.hosts})
    return {
        "rows": rows,
        "meta": {
            "resolution": res.status,
            "query": args.hostname,
            "resolved_hosts": sorted({h.hostname for h in res.hosts}),
            "monitored_by": monitored_by,
            "platforms_with_alerts": platforms_with_alerts,
            "active_only": args.active_only,
        },
    }


def get_host_status(db: Session, args: HostStatusIn) -> dict[str, Any]:
    res = resolve_hostname(db, args.hostname)
    if res.status in ("none", "ambiguous"):
        return _unresolved(res)
    monitored_by = sorted({h.source_platform.value for h in res.hosts})
    not_monitored_by = [p for p in PLATFORM_ORDER if p not in monitored_by and p in LIVE_PLATFORMS]
    return {
        "rows": _resolution_rows(res),
        "meta": {
            "resolution": res.status,
            "query": args.hostname,
            "resolved_hosts": sorted({h.hostname for h in res.hosts}),
            "monitored_by": monitored_by,
            "not_monitored_by": not_monitored_by,
        },
    }


def get_active_alerts_summary(db: Session, args: AlertsSummaryIn) -> dict[str, Any]:
    stmt = select(Alert).options(defer(Alert.raw_payload)).where(Alert.resolved.is_(False))
    if args.severity_min is not None:
        stmt = stmt.where(Alert.severity_int >= args.severity_min)
    if args.service:
        like = f"%{args.service.strip()}%"
        stmt = stmt.where(or_(Alert.host_group.ilike(like), Alert.business_service.ilike(like),
                              Alert.service_name.ilike(like), Alert.application_name.ilike(like)))
    stmt = stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc().nullslast()).limit(args.limit)
    rows = [_alert_row(a) for a in db.scalars(stmt).all()]
    by_platform: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for r in rows:
        by_platform[r["source_platform"]] = by_platform.get(r["source_platform"], 0) + 1
        by_severity[str(r["severity"])] = by_severity.get(str(r["severity"]), 0) + 1
    return {"rows": rows, "meta": {"returned": len(rows), "limit": args.limit,
                                   "by_platform": by_platform, "by_severity": by_severity,
                                   "severity_min": args.severity_min, "service": args.service}}


def get_capacity_risk(db: Session, args: CapacityRiskIn) -> dict[str, Any]:
    stmt = select(CapacityForecast, Host).join(Host, Host.id == CapacityForecast.host_id)
    meta: dict[str, Any] = {}
    if args.hostname:
        res = resolve_hostname(db, args.hostname)
        if res.status in ("none", "ambiguous"):
            return _unresolved(res)
        stmt = stmt.where(Host.id.in_([h.id for h in res.hosts]))
        meta.update(resolution=res.status, query=args.hostname,
                    resolved_hosts=sorted({h.hostname for h in res.hosts}))
    if args.classification:
        stmt = stmt.where(CapacityForecast.classification == args.classification.strip().lower())
    elif not args.hostname:
        # Estate-wide with no filter: the useful view is the risky end of the
        # table. For a named host the user wants its rows whatever their class.
        stmt = stmt.where(CapacityForecast.classification.in_(["critical", "warning", "watch"]))
    stmt = stmt.order_by(CapacityForecast.days_to_threshold_90.asc().nullslast(), Host.hostname).limit(60)
    rows = []
    for fc, host in db.execute(stmt):
        rows.append({
            "record_id": f"forecast:{fc.id}",
            "source_platform": fc.platform or host.source_platform.value,
            "source_instance": host.source_instance,
            "hostname": host.hostname,
            "resource": fc.metric_kind,
            "subject": fc.subject,
            "current_pct": fc.current_pct,
            "trend_pct_per_day": fc.slope_pct_per_day,
            "days_to_90pct": fc.days_to_threshold_90,
            "days_to_full": fc.days_to_full,
            "classification": fc.classification,
            "confidence_r2": fc.r_squared,
            "reason": fc.reason,
            "computed_at": _iso(fc.computed_at),
        })
    meta.update(classification=args.classification, returned=len(rows))
    return {"rows": rows, "meta": meta}


# --- Registry ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: type[BaseModel]
    fn: Callable[[Session, Any], dict[str, Any]]


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec for spec in (
        ToolSpec(
            "get_host_alerts",
            "Alerts for one host across every monitoring platform SAMIX holds (Zabbix, Dynatrace, "
            "NNMi, SiteScope). Use for 'what problems are on X'. Returns active alerts by default.",
            HostAlertsIn, get_host_alerts,
        ),
        ToolSpec(
            "get_host_status",
            "Host rows for one host from every platform: status, last seen, group/service, which "
            "platforms monitor it and which do not. Use for 'is X monitored', 'is X up'.",
            HostStatusIn, get_host_status,
        ),
        ToolSpec(
            "get_active_alerts_summary",
            "Top current alerts across the estate, most severe first. Optional minimum severity "
            "(1-5) and service/group filter. Use for 'what is happening now', 'worst alerts'.",
            AlertsSummaryIn, get_active_alerts_summary,
        ),
        ToolSpec(
            "get_capacity_risk",
            "Capacity forecast rows: which disks/memory are filling up, days to 90%, classification "
            "(critical/warning/watch/ok) and confidence. Optional host or classification filter.",
            CapacityRiskIn, get_capacity_risk,
        ),
    )
}


def ollama_tool_definitions() -> list[dict[str, Any]]:
    """The allow-list, in the shape Ollama's chat API expects."""
    out = []
    for spec in TOOLS.values():
        schema = spec.schema.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        out.append({"type": "function", "function": {
            "name": spec.name, "description": spec.description, "parameters": schema,
        }})
    return out


@dataclass
class ToolResult:
    name: str
    args: dict[str, Any]
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    total_rows: int = 0
    error: str | None = None
    elapsed_ms: int = 0

    def for_model(self) -> dict[str, Any]:
        """Compact form fed back to the model as the tool message."""
        return {"tool": self.name, "ok": self.ok, "error": self.error,
                "row_count": len(self.rows), "truncated": self.truncated,
                "meta": self.meta, "rows": self.rows}


class ToolBroker:
    """Runs tools safely. One instance per process is fine; it holds no state
    beyond its settings."""

    def __init__(self, settings: Settings, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.timeout = float(getattr(settings, "ai_tool_timeout_seconds", 10))
        self.max_rows = int(getattr(settings, "ai_tool_max_rows", 50))
        self._session_factory = session_factory
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai-tool")

    def allowed(self, name: str) -> bool:
        return name in TOOLS

    def execute(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        started = time.monotonic()
        spec = TOOLS.get(name)
        if spec is None:
            raise UnknownToolError(f"tool '{name}' is not on the allow-list")
        try:
            parsed = spec.schema.model_validate(args or {})
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise ToolArgumentError(f"{name}: invalid arguments ({problems})") from exc

        def _run() -> dict[str, Any]:
            db = self._session_factory()
            try:
                return spec.fn(db, parsed)
            finally:
                db.close()

        future = self._pool.submit(_run)
        try:
            result = future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return ToolResult(name, parsed.model_dump(), False, error=f"{name} timed out after {self.timeout:g}s",
                              elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - a tool failure is evidence, not a crash
            logger.exception("ai tool %s failed", name)
            return ToolResult(name, parsed.model_dump(), False, error=f"{name} failed: {exc.__class__.__name__}",
                              elapsed_ms=int((time.monotonic() - started) * 1000))

        rows = list(result.get("rows") or [])
        total = len(rows)
        truncated = total > self.max_rows
        return ToolResult(
            name, parsed.model_dump(), True, rows[: self.max_rows], dict(result.get("meta") or {}),
            truncated, total, None, int((time.monotonic() - started) * 1000),
        )
