"""Normalization layer: raw collector payloads -> unified ORM records.

Owns two responsibilities:

1. Severity normalization — mapping each platform's native severity onto the
   shared 1..5 integer scale (1=info .. 5=disaster) plus a human label.
2. Upsert / reconciliation — inserting or updating :class:`Host` and
   :class:`Alert` rows keyed on ``(source_platform, external_id)``, and
   reconciling records missing from the latest run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, Host, HostStatus, SourcePlatform

# --- Severity scale ---------------------------------------------------------

# Neutral, cross-platform labels for the aggregate severity rollup (the donut).
# Deliberately NOT Zabbix wording ("disaster"/"average") — those are shown only
# on Zabbix's own alerts via the native labels below.
SEVERITY_LABELS: dict[int, str] = {
    1: "info",
    2: "low",
    3: "medium",
    4: "high",
    5: "critical",
}

# --- Native (per-tool) severity labels --------------------------------------
# Each alert carries its source tool's own severity label, shown verbatim in the
# UI. The 1..5 int above is only for sorting and the cross-platform donut.

ZABBIX_SEVERITY_LABELS: dict[int, str] = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}

_DYNATRACE_LABELS: dict[str, str] = {
    "AVAILABILITY": "Availability",
    "ERROR": "Error",
    "PERFORMANCE": "Performance",
    "RESOURCE_CONTENTION": "Resource",
    "MONITORING_UNAVAILABLE": "Monitoring unavailable",
    "CUSTOM_ALERT": "Custom alert",
    "INFO": "Info",
}

_NNMI_LABELS: dict[str, str] = {
    "CRITICAL": "Critical",
    "MAJOR": "Major",
    "MINOR": "Minor",
    "WARNING": "Warning",
    "NORMAL": "Normal",
    "INFO": "Info",
}

# Dynatrace severityLevel -> unified 1..5.
# Dynatrace has no "disaster" tier, so nothing maps to 5 — its most severe
# problems (availability / error) map to High (4).
_DYNATRACE_SEVERITY: dict[str, int] = {
    "AVAILABILITY": 4,
    "ERROR": 4,
    "PERFORMANCE": 3,
    "RESOURCE_CONTENTION": 3,
    "CUSTOM_ALERT": 2,
    "MONITORING_UNAVAILABLE": 3,
    "INFO": 1,
}

# NNMi incident severity -> unified 1..5
_NNMI_SEVERITY: dict[str, int] = {
    "CRITICAL": 5,
    "MAJOR": 4,
    "MINOR": 3,
    "WARNING": 2,
    "NORMAL": 1,
    "INFO": 1,
}


def severity_label(severity_int: int) -> str:
    """Return the human label for a unified severity integer."""
    return SEVERITY_LABELS.get(severity_int, "info")


def normalize_zabbix_severity(priority: int | str) -> int:
    """Map a Zabbix trigger priority (0..5) onto the unified 1..5 scale.

    Zabbix priorities 0 (not classified) and 1 (information) both map to 1.
    """
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, p))


def normalize_dynatrace_severity(severity_level: str | None) -> int:
    """Map a Dynatrace ``severityLevel`` onto the unified 1..5 scale."""
    if not severity_level:
        return 1
    # Cap at 4 — Dynatrace never maps to disaster (5).
    return min(4, _DYNATRACE_SEVERITY.get(severity_level.upper(), 3))


def normalize_nnmi_severity(severity: str | None) -> int:
    """Map an NNMi incident severity onto the unified 1..5 scale."""
    if not severity:
        return 1
    return _NNMI_SEVERITY.get(severity.upper(), 1)


# --- Native label helpers (shown as-is per tool) ----------------------------


def zabbix_severity_label(priority: int | str) -> str:
    """Return Zabbix's own priority label (e.g. 'Disaster', 'High')."""
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return "Information"
    return ZABBIX_SEVERITY_LABELS.get(max(0, min(5, p)), "Information")


def dynatrace_severity_label(severity_level: str | None) -> str:
    """Return Dynatrace's own severityLevel label (e.g. 'Availability')."""
    if not severity_level:
        return "Info"
    return _DYNATRACE_LABELS.get(severity_level.upper(), severity_level.title())


def nnmi_severity_label(severity: str | None) -> str:
    """Return NNMi's own incident severity label (e.g. 'Critical', 'Major')."""
    if not severity:
        return "Normal"
    return _NNMI_LABELS.get(severity.upper(), severity.title())


# --- Upsert helpers ---------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_hosts(
    db: Session,
    platform: SourcePlatform,
    hosts: list[dict],
    instance: str = "",
) -> int:
    """Insert/update host records and reconcile stale ones.

    ``hosts`` is a list of normalized dicts with keys: ``external_id``,
    ``hostname``, ``ip``, ``status`` (a :class:`HostStatus`), ``group_name``,
    ``raw_payload``. Records are scoped to ``(platform, instance)``: hosts
    previously known for this instance but absent from this run are marked
    ``unknown`` (never deleted).

    Returns the number of hosts present in this run.
    """
    now = _utcnow()
    seen_external_ids: set[str] = set()

    existing = {
        h.external_id: h
        for h in db.scalars(
            select(Host).where(
                Host.source_platform == platform,
                Host.source_instance == instance,
            )
        ).all()
    }

    for item in hosts:
        external_id = str(item["external_id"])
        seen_external_ids.add(external_id)
        row = existing.get(external_id)
        if row is None:
            row = Host(
                source_platform=platform,
                source_instance=instance,
                external_id=external_id,
            )
            db.add(row)
        row.hostname = item.get("hostname") or external_id
        row.ip = item.get("ip")
        row.status = item.get("status", HostStatus.unknown)
        row.group_name = item.get("group_name")
        row.last_seen = item.get("last_seen") or now
        # Capacity metrics: only overwrite when this run provided them, so a
        # collector that doesn't emit metrics never wipes previously-known ones.
        if "cpu_pct" in item:
            row.cpu_pct = item.get("cpu_pct")
        if "mem_pct" in item:
            row.mem_pct = item.get("mem_pct")
        if "disk_pct" in item:
            row.disk_pct = item.get("disk_pct")
        if item.get("metrics") is not None:
            row.metrics = item["metrics"]
        elif row.metrics is None:
            row.metrics = {}
        row.raw_payload = item.get("raw_payload", {})
        row.updated_at = now

    # Reconcile: hosts not present this run become unknown.
    for external_id, row in existing.items():
        if external_id not in seen_external_ids and row.status != HostStatus.unknown:
            row.status = HostStatus.unknown
            row.updated_at = now

    db.flush()
    return len(seen_external_ids)


def upsert_alerts(
    db: Session,
    platform: SourcePlatform,
    alerts: list[dict],
    instance: str = "",
) -> int:
    """Insert/update alert records and reconcile resolved ones.

    ``alerts`` is a list of normalized dicts with keys: ``external_id``,
    ``host_hostname``, ``severity_int``, ``title``, ``started_at``,
    ``raw_payload``. Records are scoped to ``(platform, instance)``: active
    alerts previously known for this instance but absent from this run are
    marked ``resolved=True``.

    Returns the number of alerts present in this run.
    """
    now = _utcnow()
    seen_external_ids: set[str] = set()

    existing = {
        a.external_id: a
        for a in db.scalars(
            select(Alert).where(
                Alert.source_platform == platform,
                Alert.source_instance == instance,
            )
        ).all()
    }

    for item in alerts:
        external_id = str(item["external_id"])
        seen_external_ids.add(external_id)
        row = existing.get(external_id)
        if row is None:
            row = Alert(
                source_platform=platform,
                source_instance=instance,
                external_id=external_id,
            )
            db.add(row)
        sev = int(item.get("severity_int", 1))
        row.severity_int = sev
        # Prefer the source tool's own label; fall back to the unified label.
        row.severity_label = item.get("severity_label") or severity_label(sev)
        row.host_hostname = item.get("host_hostname")
        row.title = item.get("title", "")
        row.started_at = item.get("started_at")
        row.resolved = False
        row.raw_payload = item.get("raw_payload", {})
        row.updated_at = now

    # Reconcile: previously-active alerts missing this run are resolved.
    for external_id, row in existing.items():
        if external_id not in seen_external_ids and not row.resolved:
            row.resolved = True
            row.updated_at = now

    db.flush()
    return len(seen_external_ids)
