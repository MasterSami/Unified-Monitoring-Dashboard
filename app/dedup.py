"""Deduplication — Correlation Phase 2.

Links :class:`~app.models.Alert` occurrences that share a fingerprint (see
``app.fingerprint``) to one :class:`~app.models.LogicalEvent`, without ever
touching the occurrences themselves — no row is merged, deleted, or has its
own source/source_event_id/original_severity altered by anything here.

Idempotency (source + source_instance + source_event_id never creates a
duplicate logical event) falls out of the design rather than needing its own
check: an occurrence is one specific ``Alert`` row, already unique on
``(source_platform, source_instance, external_id)`` by Phase 1's own upsert
semantics. Re-polling the same source event updates that SAME row in place;
:func:`record_occurrences_batch` only relinks a row when its fingerprint
actually changed since it was last linked, so re-seeing the identical alert
is a no-op here too.

Every :class:`LogicalEvent` aggregate field (occurrence_count, first_seen,
last_seen, sources, status, representative severity/title) is recomputed
from a fresh scan of its linked occurrences every time it is touched — never
hand-incremented — so stored state can never drift from what the occurrences
themselves show. Only logical events actually touched this run are
recomputed, so this stays cheap regardless of table size.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, LogicalEvent, LogicalEventStatus


def _aware(d: datetime) -> datetime:
    """Normalize to aware-UTC. Rows loaded from SQLite come back naive
    (implicitly UTC) while freshly-set values are aware — same issue
    app.normalizer._same_instant exists for; min()/max() over a mix of the
    two raises TypeError rather than silently comparing wrong.
    """
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def record_occurrences_batch(db: Session, alerts: list[Alert]) -> None:
    """(Re)link every alert in ``alerts`` that carries a fingerprint to its
    :class:`LogicalEvent`, creating one per new fingerprint, then recompute
    every logical event actually touched.

    ``alerts`` must already be flushed (have an ``id``) — callers pass the
    rows they just touched in ``upsert_alerts``/``upsert_resolved_alerts``/
    the SiteScope ingest path, after their own ``db.flush()``.
    """
    relevant = [a for a in alerts if a.fingerprint]
    if not relevant:
        return

    fingerprints = {a.fingerprint for a in relevant}
    by_fingerprint: dict[str, LogicalEvent] = {
        le.fingerprint: le
        for le in db.scalars(
            select(LogicalEvent).where(LogicalEvent.fingerprint.in_(fingerprints))
        ).all()
    }
    # An alert may be moving OFF a fingerprint it was previously linked
    # under (rare — a source reclassifying the same event) — those old
    # logical events need recomputing too, even though their fingerprint
    # isn't in this batch.
    old_ids = {a.logical_event_id for a in relevant if a.logical_event_id is not None}
    if old_ids:
        known_ids = {le.id for le in by_fingerprint.values()}
        missing = old_ids - known_ids
        if missing:
            for le in db.scalars(
                select(LogicalEvent).where(LogicalEvent.id.in_(missing))
            ).all():
                by_fingerprint.setdefault(le.fingerprint, le)

    touched: set[int] = set()

    for a in relevant:
        target = by_fingerprint.get(a.fingerprint)
        already_linked = target is not None and a.logical_event_id == target.id

        if a.logical_event_id is not None and not already_linked:
            touched.add(a.logical_event_id)  # leaving this group; it shrinks

        if not already_linked:
            if target is None:
                target = LogicalEvent(
                    fingerprint=a.fingerprint,
                    entity_id=a.entity_id,
                    normalized_problem_type=a.normalized_problem_type or "UNKNOWN",
                    status=LogicalEventStatus.open,
                )
                db.add(target)
                db.flush()  # needs its id before other alerts in this batch can link to it
                by_fingerprint[a.fingerprint] = target
            a.logical_event_id = target.id

        # Always recompute this alert's group, even when the link itself is
        # unchanged — the caller only passes rows it touched this run, and
        # something about THIS row changed (e.g. it just resolved), which
        # can flip its group's status (open/deduplicated -> resolved) without
        # its fingerprint or link changing at all.
        touched.add(target.id)

    db.flush()  # so the recompute's SELECTs see every logical_event_id just set
    _recompute_logical_events(db, touched)


def _recompute_logical_events(db: Session, logical_event_ids: set[int]) -> None:
    if not logical_event_ids:
        return
    ids = sorted(logical_event_ids)
    rows: list[LogicalEvent] = []
    occurrences_by_event: dict[int, list[Alert]] = {}
    # Chunked IN() loads (SQLite's bound-parameter cap) instead of one SELECT
    # per touched logical event: a history backfill touches thousands at once.
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows.extend(db.scalars(select(LogicalEvent).where(LogicalEvent.id.in_(chunk))).all())
        for o in db.scalars(select(Alert).where(Alert.logical_event_id.in_(chunk))).all():
            occurrences_by_event.setdefault(o.logical_event_id, []).append(o)
    for le in rows:
        occurrences = occurrences_by_event.get(le.id, [])
        if not occurrences:
            # Its only occurrence(s) moved to a different fingerprint —
            # nothing left to group. Not "resolved" (nothing happened to a
            # real condition), just gone.
            db.delete(le)
            continue

        le.occurrence_count = len(occurrences)
        starts = [_aware(o.started_at) for o in occurrences if o.started_at is not None]
        le.first_seen = min(starts) if starts else le.first_seen
        seens = [_aware(o.started_at or o.updated_at) for o in occurrences]
        le.last_seen = max(seens) if seens else le.last_seen
        le.sources = sorted({o.source_platform.value for o in occurrences})

        # Representative: the highest-severity CURRENTLY ACTIVE occurrence,
        # so an escalating-but-still-open group shows its worst active
        # state — falls back to the highest overall once everything has
        # resolved, so a resolved group still shows what it peaked at.
        active = [o for o in occurrences if not o.resolved] or occurrences
        rep = max(active, key=lambda o: o.severity_int)
        le.current_severity_int = rep.severity_int
        le.current_severity_label = rep.severity_label
        le.title = rep.title

        le.status = _compute_status(le, occurrences)


def _compute_status(le: LogicalEvent, occurrences: list[Alert]) -> LogicalEventStatus:
    """Pure function of the logical event's current data — see the
    LogicalEventStatus docstring in app/models.py for what each state means.
    """
    all_resolved = all(o.resolved for o in occurrences)
    was_resolved = le.status == LogicalEventStatus.resolved

    if all_resolved:
        return LogicalEventStatus.resolved
    if was_resolved:
        return LogicalEventStatus.reopened
    if le.occurrence_count > 1:
        return LogicalEventStatus.deduplicated
    only = occurrences[0]
    if only.original_severity is not None and only.severity_label != only.original_severity:
        return LogicalEventStatus.updated
    return LogicalEventStatus.open
