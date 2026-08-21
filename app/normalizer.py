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


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    """True if two datetimes name the same instant.

    Rows loaded from SQLite come back naive (implicitly UTC) while collector
    values are UTC-aware, so a plain ``==`` never matches — normalize both to
    aware-UTC first.
    """
    if a is None or b is None:
        return a is b

    def norm(d: datetime) -> datetime:
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)

    return norm(a) == norm(b)


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
        # Owner: only overwrite when this run resolved one, so a collector that
        # can't resolve owners never wipes a previously-known owner.
        if item.get("owner") is not None:
            row.owner = item.get("owner")
        if item.get("owner_email") is not None:
            row.owner_email = item.get("owner_email")
        row.last_seen = item.get("last_seen") or now
        # Capacity metrics: only overwrite when this run provided them, so a
        # collector that doesn't emit metrics never wipes previously-known ones.
        if "cpu_pct" in item:
            row.cpu_pct = item.get("cpu_pct")
        if "mem_pct" in item:
            row.mem_pct = item.get("mem_pct")
        if "disk_pct" in item:
            row.disk_pct = item.get("disk_pct")
        # A host can be ``unknown`` at the availability layer while Zabbix
        # still has (and continues to show) its historical item values.  A
        # transient empty metric pass must not erase the last good Capacity
        # snapshot in that case.  Only replace the JSON metrics when the
        # collector actually supplied a capacity value; initialize a new row
        # to an empty mapping as before.
        has_capacity_update = (
            any(key in item for key in ("cpu_pct", "mem_pct", "disk_pct"))
            or bool(item.get("metrics"))
        )
        if has_capacity_update:
            row.metrics = item.get("metrics") or {}
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
        row.host_external_id = item.get("host_external_id")
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


def upsert_resolved_alerts(
    db: Session,
    platform: SourcePlatform,
    alerts: list[dict],
    instance: str = "",
) -> int:
    """Backfill RESOLVED alerts pulled from the source tool's history.

    Unlike :func:`upsert_alerts` this never reconciles or un-resolves anything —
    every row here is inserted/updated as ``resolved=True``. Dedup order:

    1. an existing row with the same ``external_id`` (e.g. a Dynatrace problem
       that was previously active) is updated in place;
    2. else, if the item carries ``match_external_id`` (e.g. the Zabbix trigger
       id behind an event) a resolved row with that id and the same
       ``started_at`` is treated as the same episode and left as-is;
    3. else a new resolved row is inserted.

    Returns the number of NEW history rows inserted.
    """
    now = _utcnow()
    if not alerts:
        return 0

    # Load ONLY the rows this batch might touch (its own external_ids plus any
    # match ids used for episode dedup), chunked to respect SQLite's bound-param
    # limit. Loading the whole (potentially 100k+ row) history every run was the
    # main backfill cost.
    wanted_ids: set[str] = set()
    for item in alerts:
        wanted_ids.add(str(item["external_id"]))
        mid = item.get("match_external_id")
        if mid is not None:
            wanted_ids.add(str(mid))

    existing: dict[str, Alert] = {}
    id_list = list(wanted_ids)
    for i in range(0, len(id_list), 500):
        chunk = id_list[i : i + 500]
        for a in db.scalars(
            select(Alert).where(
                Alert.source_platform == platform,
                Alert.source_instance == instance,
                Alert.external_id.in_(chunk),
            )
        ).all():
            existing[a.external_id] = a
    inserted = 0
    for item in alerts:
        external_id = str(item["external_id"])
        row = existing.get(external_id)
        if row is None:
            match_id = item.get("match_external_id")
            if match_id is not None:
                twin = existing.get(str(match_id))
                if (
                    twin is not None
                    and twin.resolved
                    and _same_instant(twin.started_at, item.get("started_at"))
                ):
                    continue  # same episode already recorded via reconciliation
            row = Alert(
                source_platform=platform,
                source_instance=instance,
                external_id=external_id,
            )
            db.add(row)
            existing[external_id] = row
            inserted += 1
        sev = int(item.get("severity_int", 1))
        row.severity_int = sev
        row.severity_label = item.get("severity_label") or severity_label(sev)
        row.host_hostname = item.get("host_hostname")
        row.title = item.get("title", "")
        row.started_at = item.get("started_at")
        row.resolved = True
        row.raw_payload = item.get("raw_payload", {})
        row.updated_at = now
    db.flush()
    return inserted

