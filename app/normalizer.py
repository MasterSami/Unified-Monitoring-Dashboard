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

from app.canonical_event import adapt_event
from app.dedup import record_occurrences_batch
from app.entity_resolution import resolve_hosts_batch
from app.fingerprint import compute_fingerprint, normalize_problem_type
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
    #: Rows touched this run, so capacity sampling can reach their ids after
    #: the flush without re-querying.
    touched: dict[str, Host] = {}
    #: Hosts whose identity actually needs (re-)resolving this run — see the
    #: identity_unchanged check below. Resolved together, once, after the
    #: main loop (app.entity_resolution.resolve_hosts_batch), not one at a
    #: time, so a steady-state poll of an unchanged estate costs zero extra
    #: queries and even a fully-new batch costs a small constant number.
    needs_resolution: list[dict] = []

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
        touched[external_id] = row
        row.hostname = item.get("hostname") or external_id
        row.ip = item.get("ip")
        # Only Dynatrace populates this (a multi-homed host reports several
        # addresses); leaving it untouched elsewhere keeps the column NULL for
        # platforms that only ever have one, same convention as agent_deployed.
        if "ip_all" in item:
            row.ip_all = item.get("ip_all")
        row.status = item.get("status", HostStatus.unknown)
        row.group_name = item.get("group_name")
        # Owner: only overwrite when this run resolved one, so a collector that
        # can't resolve owners never wipes a previously-known owner.
        if item.get("owner") is not None:
            row.owner = item.get("owner")
        if item.get("owner_email") is not None:
            row.owner_email = item.get("owner_email")
        # Only Dynatrace reports this; leaving it untouched elsewhere keeps the
        # column NULL for platforms where the distinction does not exist.
        if "agent_deployed" in item:
            row.agent_deployed = item.get("agent_deployed")
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

        # Entity resolution (Correlation Phase 1). Queued for the batched
        # resolve below, skipped entirely when nothing this row is matched
        # on has changed and it already resolved once — the overwhelming
        # majority of a steady-state poll, since identity signals rarely
        # change between one run and the next.
        new_fqdn = item.get("fqdn")
        new_cmdb_id = item.get("cmdb_id") or (item.get("raw_payload") or {}).get("serial")
        identity_unchanged = (
            row.entity_id is not None
            and row.ip == item.get("ip")
            and row.hostname == (item.get("hostname") or external_id)
            and row.fqdn == new_fqdn
        )
        if "fqdn" in item:
            row.fqdn = new_fqdn
        if new_cmdb_id:
            row.cmdb_id = new_cmdb_id
        if not identity_unchanged:
            needs_resolution.append({
                "external_id": external_id,
                "hostname": row.hostname,
                "ip": row.ip,
                "ip_all": row.ip_all,
                "fqdn": row.fqdn,
                "cmdb_id": row.cmdb_id,
                "prior_entity_id": row.entity_id,
            })

    # Reconcile: hosts not present this run become unknown.
    for external_id, row in existing.items():
        if external_id not in seen_external_ids and row.status != HostStatus.unknown:
            row.status = HostStatus.unknown
            row.updated_at = now

    if needs_resolution:
        results = resolve_hosts_batch(
            db, platform=platform, instance=instance, items=needs_resolution
        )
        for external_id, result in results.items():
            row = touched[external_id]
            row.entity_id = result.entity_id
            row.resolution_method = result.method.value
            row.resolution_confidence = result.confidence

    db.flush()

    # Append this run's capacity readings to the history table. The flush above
    # is what gives new hosts their ids. Contained: a sampling failure is logged
    # and never costs the caller its host upsert.
    from app.capacity_history import record_samples

    record_samples(db, platform.value, hosts, touched, now=now)

    return len(seen_external_ids)


def _lookup_hosts_for_alerts(
    db: Session, platform: SourcePlatform, instance: str, alerts: list[dict]
) -> tuple[dict[str, Host], dict[str, Host]]:
    """Batched Host lookup for the entity/owner context alerts inherit.

    Alerts reference their host by ``host_external_id`` when the source
    provides one (Zabbix, Dynatrace), or only by ``host_hostname`` when it
    does not (NNMi, SiteScope) — so both indexes are built from one query.
    One round trip for the whole batch, not one per alert.
    """
    external_ids = {
        str(a["host_external_id"]) for a in alerts if a.get("host_external_id")
    }
    hostnames = {
        str(a["host_hostname"]).strip().lower()
        for a in alerts if a.get("host_hostname")
    }
    if not external_ids and not hostnames:
        return {}, {}
    rows = db.scalars(
        select(Host).where(
            Host.source_platform == platform,
            Host.source_instance == instance,
        )
    ).all()
    by_external_id = {h.external_id: h for h in rows if h.external_id in external_ids}
    by_hostname = {
        h.hostname.strip().lower(): h
        for h in rows if h.hostname and h.hostname.strip().lower() in hostnames
    }
    return by_external_id, by_hostname


def _apply_canonical_fields(
    row: Alert,
    item: dict,
    platform: SourcePlatform,
    by_external_id: dict[str, Host],
    by_hostname: dict[str, Host],
    *,
    is_new: bool,
    resolved: bool,
    now: datetime,
) -> None:
    """Fill the Correlation-Phase-1 canonical fields on ``row`` (in place).

    Split out of :func:`upsert_alerts`/:func:`upsert_resolved_alerts` so both
    the live and the history-backfill path enrich alerts the same way.
    """
    for field, value in adapt_event(platform, item).items():
        setattr(row, field, value)

    row.status = "resolved" if resolved else "open"
    row.last_seen = now
    row.payload_ref = f"{platform.value}:{item.get('source_instance', '')}:{row.external_id}"

    # Original values are recorded once, on first sight, and never touched
    # again — see the Alert docstring. Everything else in this function may
    # legitimately change on every poll (e.g. a host's owner changing hands).
    if is_new:
        row.original_severity = item.get("severity_label") or row.severity_label
        row.original_description = item.get("title") or row.title

    host = by_external_id.get(item.get("host_external_id"))
    if host is None and item.get("host_hostname"):
        host = by_hostname.get(str(item["host_hostname"]).strip().lower())
    if host is not None:
        row.host_ip = host.ip
        row.fqdn = host.fqdn
        row.host_group = host.group_name
        row.host_owner = host.owner
        row.entity_id = host.entity_id
        # Phase 1 only ever resolves Host rows, so this is always "host" —
        # see EntityType / the CanonicalEntity module note.
        row.entity_type = "host" if host.entity_id is not None else None

    # Fingerprinting (Correlation Phase 2) — needs entity_id, so this runs
    # after the host lookup above, not before it.
    row.normalized_problem_type = normalize_problem_type(
        problem_type=row.problem_type, title=row.title, metric_name=row.metric_name,
    )
    row.fingerprint = compute_fingerprint(
        entity_id=row.entity_id,
        normalized_problem_type=row.normalized_problem_type,
        metric_name=row.metric_name,
        api_id=row.api_id,
        service_id=row.service_id,
        database_id=row.database_id,
        network_device_id=row.network_device_id,
    )


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
    touched_rows: list[Alert] = []

    # Only the instance's OPEN rows need reconciling, plus any row whose
    # external_id this run reports again (a resolved episode the source has
    # re-raised under the same id is reopened in place, as before). Loading
    # every row for the instance — including the resolved history, which on
    # a busy Zabbix is tens of thousands of rows — was the single biggest
    # cost of a poll, paid every five minutes for nothing.
    existing = {
        a.external_id: a
        for a in db.scalars(
            select(Alert).where(
                Alert.source_platform == platform,
                Alert.source_instance == instance,
                Alert.resolved.is_(False),
            )
        ).all()
    }
    reported_ids = [str(item["external_id"]) for item in alerts]
    missing = [eid for eid in set(reported_ids) if eid not in existing]
    for i in range(0, len(missing), 500):
        for a in db.scalars(
            select(Alert).where(
                Alert.source_platform == platform,
                Alert.source_instance == instance,
                Alert.external_id.in_(missing[i : i + 500]),
            )
        ).all():
            existing[a.external_id] = a
    by_external_id, by_hostname = _lookup_hosts_for_alerts(db, platform, instance, alerts)

    for item in alerts:
        external_id = str(item["external_id"])
        seen_external_ids.add(external_id)
        row = existing.get(external_id)
        is_new = row is None
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
        _apply_canonical_fields(
            row, {**item, "source_instance": instance}, platform,
            by_external_id, by_hostname, is_new=is_new, resolved=False, now=now,
        )
        touched_rows.append(row)

    # Reconcile: previously-active alerts missing this run are resolved.
    for external_id, row in existing.items():
        if external_id not in seen_external_ids and not row.resolved:
            row.resolved = True
            row.resolved_at = now
            row.status = "resolved"
            row.updated_at = now
            # Its LogicalEvent's status (open/deduplicated -> resolved) needs
            # recomputing even though this row's own fingerprint is unchanged.
            touched_rows.append(row)

    db.flush()
    record_occurrences_batch(db, touched_rows)
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
    by_external_id, by_hostname = _lookup_hosts_for_alerts(db, platform, instance, alerts)
    inserted = 0
    touched_rows: list[Alert] = []
    for item in alerts:
        external_id = str(item["external_id"])
        row = existing.get(external_id)
        is_new = row is None
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
        elif row.resolved:
            # Already stored as resolved history: a closed episode never
            # changes again, so re-writing every field (and re-running the
            # logical-event recompute) for each of the tens of thousands of
            # rows a full-window scan returns is pure cost. Only a row that
            # was still OPEN when history caught up with it (the Dynatrace
            # case below) needs updating in place.
            continue
        sev = int(item.get("severity_int", 1))
        row.severity_int = sev
        row.severity_label = item.get("severity_label") or severity_label(sev)
        row.host_hostname = item.get("host_hostname")
        row.host_external_id = item.get("host_external_id")
        row.title = item.get("title", "")
        row.started_at = item.get("started_at")
        row.resolved = True
        # Set once, like original_severity — a resolved-history row's own
        # resolution time is never in this dict (the source tools polled here
        # give no separate "resolved at" field), so "when SAMI'X first
        # recorded it resolved" is the closest honest answer, not re-stamped
        # on every re-backfill of the same row.
        row.resolved_at = row.resolved_at or now
        row.raw_payload = item.get("raw_payload", {})
        row.updated_at = now
        _apply_canonical_fields(
            row, {**item, "source_instance": instance}, platform,
            by_external_id, by_hostname, is_new=is_new, resolved=True, now=now,
        )
        touched_rows.append(row)
    db.flush()
    record_occurrences_batch(db, touched_rows)
    return inserted

