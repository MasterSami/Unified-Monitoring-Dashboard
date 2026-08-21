"""Shared SiteScope ingest: redacted log lines -> Alert + Host rows.

Used by BOTH the push endpoint (``routers/api.py``) and the optional local
demo collector (``scheduler.py``), so the exact same normalization / idempotent
upsert path runs whether events arrive over HTTP from the on-box forwarder or
are read from a redacted file on disk during a laptop demo.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, Host, HostStatus, SourcePlatform
from app.sitescope import (
    DerivedHost,
    NormalizedEvent,
    ParseError,
    derive_hosts,
    parse_line,
    redact,
)


@dataclass
class IngestCounts:
    """Outcome of ingesting one batch of lines."""

    received: int = 0
    events: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    redactions: int = 0
    hosts: int = 0


#: SQLite caps bound parameters per statement; chunk IN() lists below it.
_IN_CHUNK = 500


def _prefetch(db: Session, model, instance_col, id_col, wanted: set[tuple[str, str]]):
    """Load existing rows for a batch in chunked IN() queries.

    Keyed ``(instance, external_id) -> row``. This mirrors what
    :mod:`app.normalizer` does for the pull collectors: one query per chunk
    instead of one per item. A 500-event push used to mean 500 round trips.
    """
    found: dict[tuple[str, str], object] = {}
    ids = sorted({eid for _inst, eid in wanted})
    instances = sorted({inst for inst, _eid in wanted})
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i : i + _IN_CHUNK]
        for row in db.scalars(
            select(model).where(
                model.source_platform == SourcePlatform.sitescope,
                instance_col.in_(instances),
                id_col.in_(chunk),
            )
        ).all():
            found[(row.source_instance, row.external_id)] = row
    return found


def upsert_events(db: Session, events: list[NormalizedEvent]) -> tuple[int, int]:
    """Idempotent upsert on (platform, instance, external_id). No reconciliation.

    Push/file ingest only ever sees a batch — never the full active set — so
    unlike the pull collectors we must NOT resolve rows absent from the batch.
    """
    inserted = updated = 0
    if not events:
        return 0, 0

    # Two lines can map to the same event_id (same monitor + state in the same
    # second). Track rows touched this batch so a duplicate updates in place
    # instead of inserting a second row that would violate the unique key.
    batch: dict[tuple[str, str], Alert] = _prefetch(  # type: ignore[assignment]
        db, Alert, Alert.source_instance, Alert.external_id,
        {(ev.source_instance, ev.external_id) for ev in events},
    )
    updated = len(batch)

    for ev in events:
        key = (ev.source_instance, ev.external_id)
        row = batch.get(key)
        if row is None:
            row = Alert(
                source_platform=SourcePlatform.sitescope,
                source_instance=ev.source_instance,
                external_id=ev.external_id,
            )
            db.add(row)
            inserted += 1
            batch[key] = row
        row.host_hostname = ev.host_hostname
        row.severity_int = ev.severity_int
        row.severity_label = ev.severity_label
        row.title = ev.title
        row.started_at = ev.started_at
        row.resolved = ev.resolved
        row.state = ev.state
        row.dedup_key = ev.dedup_key
        row.metric_missing = ev.metric_missing
        row.monitor_name = ev.monitor_name
        row.raw_payload = ev.raw_payload
    return inserted, updated


def upsert_hosts(db: Session, instance: str, hosts: list[DerivedHost]) -> int:
    """Upsert derived SiteScope hosts (idempotent, no reconciliation)."""
    from datetime import datetime, timezone

    inserted = 0
    if not hosts:
        return 0
    existing = _prefetch(
        db, Host, Host.source_instance, Host.external_id,
        {(instance, h.external_id) for h in hosts},
    )
    for h in hosts:
        row = existing.get((instance, h.external_id))
        if row is None:
            row = Host(
                source_platform=SourcePlatform.sitescope,
                source_instance=instance,
                external_id=h.external_id,
            )
            db.add(row)
            existing[(instance, h.external_id)] = row
            inserted += 1
        row.hostname = h.hostname
        row.ip = h.ip
        try:
            row.status = HostStatus(h.status)
        except ValueError:
            row.status = HostStatus.unknown
        row.group_name = h.group_name
        row.last_seen = h.last_seen or datetime.now(timezone.utc)
        row.raw_payload = h.raw_payload
    return inserted


def ingest_lines(db: Session, instance: str, lines: list[str]) -> IngestCounts:
    """Parse + normalize + upsert a batch of redacted lines for ``instance``.

    Does NOT commit and does NOT record a CollectorRun — the caller owns the
    transaction and any health bookkeeping.
    """
    counts = IngestCounts(received=len(lines))
    events: list[NormalizedEvent] = []
    for line in lines:
        _, fired = redact(line)  # safety-net count (parse_line redacts too)
        counts.redactions += fired
        try:
            events.append(parse_line(line, instance))
        except ParseError:
            counts.skipped += 1

    counts.events = len(events)
    counts.inserted, counts.updated = upsert_events(db, events)
    hosts = derive_hosts(events)
    counts.hosts = len(hosts)
    upsert_hosts(db, instance, hosts)
    return counts
