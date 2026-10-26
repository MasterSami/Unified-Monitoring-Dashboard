"""Cross-platform alert correlation engine (v2).

Groups active alerts into **Correlation Groups** by matching the physical
device, distributed trace, or service dependency — with Dynatrace data as
the primary intelligence source.

Signals (strongest first):

1. **Same Trace** — Dynatrace ``trace_id`` links two alerts (highest
   confidence: the tracing system saw the actual call chain).
2. **Service Dependency** — Dynatrace ``calls``/``depends_on``/``hosted_on``
   edges in :class:`EntityRelationship` connect the entities behind two
   alerts (the topology graph proves a real dependency).
3. **Same Device** — two alerts resolve to the same physical machine via
   ``dedup_key()`` (hostname normalisation) or shared IP address, across any
   combination of platforms.
4. **Same Service / Application** — two alerts carry the same non-null
   ``service_name`` or ``application_name`` (Dynatrace enrichment).
5. **Database Impact** — a Zabbix/SiteScope alert on a DB host is linked to
   Dynatrace service alerts whose ``db_calls`` reference that host.

Temporal proximity alone never creates a group.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    EntityRelationship,
    Host,
    RelationshipType,
    SourcePlatform,
)
from app.sitescope import dedup_key

# Seconds: alerts must be within this window to be grouped on the same device.
_TIME_WINDOW = 1800  # 30 minutes

# Dependency edge types that mean "to" can break "from".
_DEPENDENCY_EDGES = frozenset({
    RelationshipType.depends_on,
    RelationshipType.calls,
    RelationshipType.hosted_on,
    RelationshipType.connects_to,
    RelationshipType.routes_to,
    RelationshipType.uses,
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CorrelationReason:
    signal: str
    detail: str
    confidence: str  # high / medium / low


@dataclass
class RootCauseCandidate:
    alert_id: int
    title: str
    host: str
    platform: str
    instance: str
    started_at: datetime | None
    severity_int: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class CorrelationGroup:
    group_id: str
    title: str
    severity_int: int
    severity_label: str
    status: str  # open / resolved
    started_at: datetime | None
    duration_seconds: int | None
    alert_ids: list[int] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    root_cause: RootCauseCandidate | None = None
    reasons: list[CorrelationReason] = field(default_factory=list)
    device_names: dict[str, str] = field(default_factory=dict)
    affected_services: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _alert_dict(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id,
        "title": a.title,
        "host": a.host_hostname or "",
        "host_ip": a.host_ip or "",
        "platform": a.source_platform.value if isinstance(a.source_platform, SourcePlatform) else str(a.source_platform),
        "instance": a.source_instance or "",
        "severity_int": a.severity_int,
        "severity_label": a.severity_label,
        "started_at": a.started_at,
        "resolved": a.resolved,
        "resolved_at": a.resolved_at,
        "trace_id": a.trace_id,
        "service_name": a.service_name,
        "application_name": a.application_name,
        "business_service": a.business_service,
        "host_group": a.host_group,
        "database_name": a.database_name,
        "db_calls": a.db_calls or [],
        "external_calls": a.external_calls or [],
    }


def _severity_label(sev: int) -> str:
    return {5: "Critical", 4: "High", 3: "Average", 2: "Warning", 1: "Info"}.get(sev, "Unknown")


def _device_key(alert: Alert) -> str:
    """Normalised host identifier: dedup_key of hostname, or IP as fallback."""
    dk = dedup_key(alert.host_hostname)
    return dk if dk else (alert.host_ip or "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt is None:
        return _now()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Step 1: Build the device map (host normalisation + IP matching)
# ---------------------------------------------------------------------------

def _build_device_map(alerts: list[Alert], db: Session) -> dict[str, set[int]]:
    """Map a canonical device key → set of alert IDs on that device.

    Two alerts land on the same device when:
    * their ``dedup_key(hostname)`` matches, OR
    * their ``host_ip`` matches, OR
    * the Host table shows both IPs belong to the same host row.
    """
    key_to_aids: dict[str, set[int]] = defaultdict(set)
    # Collect all IPs to resolve host overlaps.
    ip_to_keys: dict[str, set[str]] = defaultdict(set)

    for a in alerts:
        dk = _device_key(a)
        if dk:
            key_to_aids[dk].add(a.id)
        ip = (a.host_ip or "").strip()
        if ip:
            ip_to_keys[ip].add(dk if dk else f"ip:{ip}")
            key_to_aids.setdefault(dk if dk else f"ip:{ip}", set()).add(a.id)

    # Merge keys that share an IP.
    union: dict[str, str] = {}  # key → canonical

    def find(k: str) -> str:
        while union.get(k, k) != k:
            k = union[k]
        return k

    def merge(a_key: str, b_key: str) -> None:
        ra, rb = find(a_key), find(b_key)
        if ra != rb:
            union[rb] = ra

    for ip, keys in ip_to_keys.items():
        keys_list = sorted(keys)
        for i in range(1, len(keys_list)):
            merge(keys_list[0], keys_list[i])

    # Also check the Host table for IP overlaps across platforms.
    all_ips = [ip for ip in ip_to_keys if ip]
    if all_ips:
        hosts_by_ip: dict[str, list[str]] = defaultdict(list)
        for h in db.scalars(select(Host).where(Host.ip.in_(all_ips))):
            dk = dedup_key(h.hostname)
            if dk and h.ip:
                hosts_by_ip[h.ip].append(dk)
        for ip, dks in hosts_by_ip.items():
            for dk in dks:
                key_to_aids.setdefault(dk, set())
            for i in range(1, len(dks)):
                merge(dks[0], dks[i])
            # Also merge with alert-derived keys for this IP.
            for alert_dk in ip_to_keys.get(ip, set()):
                if dks:
                    merge(dks[0], alert_dk)

    # Rebuild merged device map.
    merged: dict[str, set[int]] = defaultdict(set)
    for dk, aids in key_to_aids.items():
        merged[find(dk)] |= aids
    return {k: v for k, v in merged.items() if len(v) > 0}


# ---------------------------------------------------------------------------
# Step 2: Trace-based grouping (Dynatrace distributed traces)
# ---------------------------------------------------------------------------

def _build_trace_groups(alerts: list[Alert]) -> dict[str, set[int]]:
    """Map trace_id → alert IDs that share it.  Only Dynatrace populates
    ``trace_id``, so any group here is high-confidence."""
    traces: dict[str, set[int]] = defaultdict(set)
    for a in alerts:
        if a.trace_id:
            traces[a.trace_id].add(a.id)
    return {t: aids for t, aids in traces.items() if len(aids) >= 2}


# ---------------------------------------------------------------------------
# Step 3: Dependency-based grouping (Dynatrace topology)
# ---------------------------------------------------------------------------

def _build_dependency_links(alerts: list[Alert], db: Session) -> list[tuple[int, int, str]]:
    """Return (alert_a_id, alert_b_id, edge_description) for alerts whose
    entities are linked by a dependency relationship."""
    entity_ids = {a.entity_id for a in alerts if a.entity_id}
    if not entity_ids:
        return []
    rels = db.scalars(
        select(EntityRelationship).where(
            EntityRelationship.relationship_type.in_(list(_DEPENDENCY_EDGES)),
            EntityRelationship.from_entity_id.in_(entity_ids)
            | EntityRelationship.to_entity_id.in_(entity_ids),
        )
    ).all()
    if not rels:
        return []
    eid_to_aids: dict[int, list[int]] = defaultdict(list)
    for a in alerts:
        if a.entity_id:
            eid_to_aids[a.entity_id].append(a.id)
    links: list[tuple[int, int, str]] = []
    for r in rels:
        for a_id in eid_to_aids.get(r.from_entity_id, []):
            for b_id in eid_to_aids.get(r.to_entity_id, []):
                if a_id != b_id:
                    links.append((a_id, b_id, r.relationship_type.value if hasattr(r.relationship_type, 'value') else str(r.relationship_type)))
    return links


# ---------------------------------------------------------------------------
# Step 4: Service / application / DB impact grouping
# ---------------------------------------------------------------------------

def _build_service_groups(alerts: list[Alert]) -> dict[str, set[int]]:
    """Group by shared service_name or application_name (Dynatrace enrichment)."""
    groups: dict[str, set[int]] = defaultdict(set)
    for a in alerts:
        if a.service_name:
            groups[f"svc:{a.service_name.lower()}"].add(a.id)
        if a.application_name:
            groups[f"app:{a.application_name.lower()}"].add(a.id)
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _build_db_impact_links(alerts: list[Alert]) -> list[tuple[int, int, str]]:
    """Link DB-host alerts to service alerts whose db_calls reference them."""
    db_host_alerts: dict[str, list[int]] = defaultdict(list)
    service_alerts: list[Alert] = []
    for a in alerts:
        if a.database_name:
            db_host_alerts[a.database_name.lower()].append(a.id)
        dk = dedup_key(a.host_hostname)
        plat = a.source_platform.value if isinstance(a.source_platform, SourcePlatform) else str(a.source_platform)
        if dk and plat in ("zabbix", "sitescope"):
            db_host_alerts[dk].append(a.id)
        if a.db_calls and isinstance(a.db_calls, list):
            service_alerts.append(a)
    links: list[tuple[int, int, str]] = []
    for sa in service_alerts:
        for call in (sa.db_calls or []):
            if not isinstance(call, dict):
                continue
            target = (call.get("name") or call.get("host") or "").lower()
            dk = dedup_key(target)
            for key in (target, dk):
                for db_aid in db_host_alerts.get(key, []):
                    if db_aid != sa.id:
                        links.append((sa.id, db_aid, f"db_call:{target}"))
    return links


# ---------------------------------------------------------------------------
# Step 5: Union-find to merge all signals into final groups
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, ids: set[int]):
        self.parent = {i: i for i in ids}
        self.rank = {i: 0 for i in ids}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _merge_groups(
    alert_ids: set[int],
    device_map: dict[str, set[int]],
    trace_groups: dict[str, set[int]],
    dep_links: list[tuple[int, int, str]],
    svc_groups: dict[str, set[int]],
    db_links: list[tuple[int, int, str]],
) -> dict[int, set[int]]:
    """Union-find over all signal sources → root_id → set of alert IDs."""
    uf = _UnionFind(alert_ids)

    # Device-based (same host / same IP).
    for aids in device_map.values():
        aids_list = sorted(aids & alert_ids)
        for i in range(1, len(aids_list)):
            uf.union(aids_list[0], aids_list[i])

    # Trace-based.
    for aids in trace_groups.values():
        aids_list = sorted(aids & alert_ids)
        for i in range(1, len(aids_list)):
            uf.union(aids_list[0], aids_list[i])

    # Dependency-based.
    for a_id, b_id, _ in dep_links:
        if a_id in alert_ids and b_id in alert_ids:
            uf.union(a_id, b_id)

    # Service / app.
    for aids in svc_groups.values():
        aids_list = sorted(aids & alert_ids)
        for i in range(1, len(aids_list)):
            uf.union(aids_list[0], aids_list[i])

    # DB impact.
    for a_id, b_id, _ in db_links:
        if a_id in alert_ids and b_id in alert_ids:
            uf.union(a_id, b_id)

    # Collect.
    groups: dict[int, set[int]] = defaultdict(set)
    for aid in alert_ids:
        groups[uf.find(aid)].add(aid)
    return dict(groups)


# ---------------------------------------------------------------------------
# Step 6: Build reasons for each group
# ---------------------------------------------------------------------------

def _group_reasons(
    aids: set[int],
    alert_map: dict[int, Alert],
    device_map: dict[str, set[int]],
    trace_groups: dict[str, set[int]],
    dep_links: list[tuple[int, int, str]],
    svc_groups: dict[str, set[int]],
    db_links: list[tuple[int, int, str]],
) -> list[CorrelationReason]:
    reasons: list[CorrelationReason] = []
    seen: set[str] = set()

    # Trace.
    for tid, t_aids in trace_groups.items():
        if t_aids & aids:
            key = f"trace:{tid}"
            if key not in seen:
                seen.add(key)
                reasons.append(CorrelationReason(
                    "same_trace",
                    f"Shared Dynatrace trace {tid[:16]}…",
                    "high",
                ))

    # Dependency.
    for a_id, b_id, edge in dep_links:
        if a_id in aids and b_id in aids:
            key = f"dep:{min(a_id,b_id)}:{max(a_id,b_id)}"
            if key not in seen:
                seen.add(key)
                a, b = alert_map[a_id], alert_map[b_id]
                reasons.append(CorrelationReason(
                    "dependency",
                    f"{a.host_hostname or a.service_name or '?'} {edge} {b.host_hostname or b.service_name or '?'}",
                    "high",
                ))

    # Device.
    for dk, d_aids in device_map.items():
        overlap = d_aids & aids
        if len(overlap) >= 2:
            key = f"device:{dk}"
            if key not in seen:
                seen.add(key)
                platforms = sorted({
                    (alert_map[a].source_platform.value if isinstance(alert_map[a].source_platform, SourcePlatform) else str(alert_map[a].source_platform))
                    for a in overlap
                })
                names = sorted({alert_map[a].host_hostname or "" for a in overlap} - {""})
                detail = f"Same device {dk}"
                if len(names) > 1:
                    detail += f" (known as {', '.join(names)})"
                if len(platforms) > 1:
                    detail += f" across {', '.join(platforms)}"
                reasons.append(CorrelationReason("same_device", detail, "high"))

    # Service / App.
    for sk, s_aids in svc_groups.items():
        overlap = s_aids & aids
        if len(overlap) >= 2:
            key = f"svc:{sk}"
            if key not in seen:
                seen.add(key)
                reasons.append(CorrelationReason(
                    "same_service",
                    f"Same {sk.split(':')[0]} '{sk.split(':',1)[1]}'",
                    "medium",
                ))

    # DB impact.
    for a_id, b_id, detail in db_links:
        if a_id in aids and b_id in aids:
            key = f"db:{min(a_id,b_id)}:{max(a_id,b_id)}"
            if key not in seen:
                seen.add(key)
                reasons.append(CorrelationReason(
                    "db_impact",
                    f"Database call chain: {detail}",
                    "medium",
                ))

    return reasons


# ---------------------------------------------------------------------------
# Step 7: Root cause ranking
# ---------------------------------------------------------------------------

def _pick_root_cause(
    aids: set[int],
    alert_map: dict[int, Alert],
    dep_links: list[tuple[int, int, str]],
) -> RootCauseCandidate | None:
    if not aids:
        return None
    group_alerts = [alert_map[a] for a in aids]

    # Build upstream score: if B depends_on A and both are in the group,
    # A gets +1 (it's more likely the root cause).
    upstream_score: dict[int, int] = defaultdict(int)
    for a_id, b_id, _ in dep_links:
        if a_id in aids and b_id in aids:
            upstream_score[b_id] += 1  # b_id is upstream (depended upon)

    # Sort: upstream score desc, then earliest started_at, then highest severity.
    ranked = sorted(
        group_alerts,
        key=lambda a: (
            -upstream_score.get(a.id, 0),
            _aware(a.started_at),
            -a.severity_int,
        ),
    )
    best = ranked[0]
    reasons: list[str] = []
    if upstream_score.get(best.id, 0) > 0:
        reasons.append("upstream dependency (other alerts depend on this)")
    if best.started_at and all(
        _aware(a.started_at) >= _aware(best.started_at) for a in group_alerts
    ):
        reasons.append("earliest alert in the group")
    if best.severity_int >= max(a.severity_int for a in group_alerts):
        reasons.append("highest severity")

    return RootCauseCandidate(
        alert_id=best.id,
        title=best.title,
        host=best.host_hostname or "",
        platform=best.source_platform.value if isinstance(best.source_platform, SourcePlatform) else str(best.source_platform),
        instance=best.source_instance or "",
        started_at=best.started_at,
        severity_int=best.severity_int,
        reasons=reasons or ["earliest alert"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def correlate_alerts(
    db: Session,
    *,
    state: str = "active",
    platform: str = "all",
    instance: str = "all",
    min_severity: int = 1,
    time_window: int = _TIME_WINDOW,
) -> tuple[list[CorrelationGroup], list[dict[str, Any]]]:
    """Run correlation over alerts and return (groups, uncorrelated_alerts).

    ``state`` filters lifecycle (active / resolved / all).
    ``platform`` and ``instance`` narrow the source.
    ``min_severity`` keeps only alerts at or above this level.

    Returns a list of :class:`CorrelationGroup` (multi-alert clusters) plus
    a list of alert dicts that didn't match any group.
    """
    # --- Fetch alerts ---
    stmt = select(Alert)
    if state == "active":
        stmt = stmt.where(Alert.resolved.is_(False))
    elif state == "resolved":
        stmt = stmt.where(Alert.resolved.is_(True))
    if platform and platform != "all":
        stmt = stmt.where(Alert.source_platform == platform)
    if instance and instance != "all":
        stmt = stmt.where(Alert.source_instance == instance)
    if min_severity > 1:
        stmt = stmt.where(Alert.severity_int >= min_severity)
    stmt = stmt.order_by(Alert.severity_int.desc(), Alert.started_at.desc())
    alerts = list(db.scalars(stmt).all())
    if not alerts:
        return [], []

    alert_map: dict[int, Alert] = {a.id: a for a in alerts}
    all_ids = set(alert_map.keys())

    # --- Compute all signals (each step is individually safe) ---
    import logging
    _log = logging.getLogger(__name__)

    try:
        device_map = _build_device_map(alerts, db)
    except Exception:
        _log.exception("device_map failed")
        device_map = {}
    trace_groups = _build_trace_groups(alerts)
    try:
        dep_links = _build_dependency_links(alerts, db)
    except Exception:
        _log.exception("dep_links failed")
        dep_links = []
    svc_groups = _build_service_groups(alerts)
    try:
        db_links = _build_db_impact_links(alerts)
    except Exception:
        _log.exception("db_links failed")
        db_links = []

    # --- Merge into groups ---
    raw_groups = _merge_groups(all_ids, device_map, trace_groups, dep_links, svc_groups, db_links)

    # --- Build output ---
    correlated_ids: set[int] = set()
    groups: list[CorrelationGroup] = []
    gnum = 0

    # Sort groups: highest severity first, then most alerts, then earliest.
    sorted_groups = sorted(
        raw_groups.items(),
        key=lambda kv: (
            -max(alert_map[a].severity_int for a in kv[1]),
            -len(kv[1]),
            min(_aware(alert_map[a].started_at) for a in kv[1]),
        ),
    )

    for root_id, aids in sorted_groups:
        if len(aids) < 2:
            continue
        gnum += 1
        correlated_ids |= aids
        group_alerts_sorted = sorted(
            [alert_map[a] for a in aids],
            key=lambda a: _aware(a.started_at),
        )
        max_sev = max(a.severity_int for a in group_alerts_sorted)
        platforms_in = sorted({
            a.source_platform.value if isinstance(a.source_platform, SourcePlatform) else str(a.source_platform)
            for a in group_alerts_sorted
        })
        first = group_alerts_sorted[0]
        last = group_alerts_sorted[-1]
        start = first.started_at
        dur = None
        if start:
            end = _aware(last.resolved_at)
            dur = int((end - _aware(start)).total_seconds())

        any_active = any(not a.resolved for a in group_alerts_sorted)

        # Device names mapping (platform → hostname as that platform knows it).
        dnames: dict[str, str] = {}
        for a in group_alerts_sorted:
            p = a.source_platform.value if isinstance(a.source_platform, SourcePlatform) else str(a.source_platform)
            if a.host_hostname and p not in dnames:
                dnames[p] = a.host_hostname

        # Affected services: collect service_name, application_name,
        # business_service (management zone), host_group from all alerts.
        aff_svcs: list[str] = []
        _seen_svc: set[str] = set()
        for a in group_alerts_sorted:
            for val in (a.business_service, a.service_name, a.application_name, a.host_group):
                if val and val not in _seen_svc:
                    _seen_svc.add(val)
                    aff_svcs.append(val)

        reasons = _group_reasons(aids, alert_map, device_map, trace_groups, dep_links, svc_groups, db_links)
        root_cause = _pick_root_cause(aids, alert_map, dep_links)

        # Title: prefer affected service name over raw hostname.
        title_label = aff_svcs[0] if aff_svcs else (root_cause.host if root_cause else (first.host_hostname or ""))
        title = f"{title_label}: {root_cause.title}" if root_cause else first.title

        groups.append(CorrelationGroup(
            group_id=f"CG-{gnum:03d}",
            title=title,
            severity_int=max_sev,
            severity_label=_severity_label(max_sev),
            status="open" if any_active else "resolved",
            started_at=start,
            duration_seconds=dur,
            alert_ids=sorted(aids),
            alerts=[_alert_dict(a) for a in group_alerts_sorted],
            platforms=platforms_in,
            root_cause=root_cause,
            reasons=reasons,
            device_names=dnames,
            affected_services=aff_svcs,
        ))

    # Uncorrelated.
    uncorrelated = [
        _alert_dict(alert_map[a])
        for a in sorted(all_ids - correlated_ids)
    ]
    return groups, uncorrelated
