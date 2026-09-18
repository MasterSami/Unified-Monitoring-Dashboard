"""Correlation engine metrics — Correlation Phase 6.

Every metric the task asks for except two is a live COUNT over tables that
already exist — same "pure function of current data" discipline as the rest
of this engine, so a metric can never drift from what the tables actually
show. Only ``correlation_failures`` and ``average_processing_time`` describe
process BEHAVIOR (something that happened, not current state) and need real
instrumentation — see :class:`~app.models.EngineMetric` and
:func:`record_duration`/:func:`increment_counter`, called from
app.correlation_engine's batch-running functions.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Alert, Correlation, EngineMetric, Incident, IncidentStatus, LogicalEvent


def increment_counter(db: Session, name: str, *, amount: float = 1.0) -> EngineMetric:
    """Bump a named counter's running sum and observation count by one."""
    row = db.scalar(select(EngineMetric).where(EngineMetric.name == name))
    if row is None:
        row = EngineMetric(name=name)
        db.add(row)
        db.flush()
    row.value += amount
    row.count += 1
    db.flush()
    return row


def record_duration(db: Session, name: str, seconds: float) -> EngineMetric:
    """Same as :func:`increment_counter`, named for call sites that are
    timing something rather than merely counting it — ``value / count`` at
    read time is that metric's average.
    """
    return increment_counter(db, name, amount=seconds)


def _stored_metric(db: Session, name: str) -> EngineMetric | None:
    return db.scalar(select(EngineMetric).where(EngineMetric.name == name))


def get_correlation_metrics(db: Session) -> dict[str, float]:
    """The task's own named metric list (section 13)."""
    events_received = db.scalar(select(func.count()).select_from(Alert)) or 0
    events_normalized = db.scalar(
        select(func.count()).select_from(Alert).where(Alert.normalized_problem_type.is_not(None))
    ) or 0
    events_deduplicated = db.scalar(select(func.count()).select_from(LogicalEvent)) or 0
    correlations_created = db.scalar(select(func.count()).select_from(Correlation)) or 0
    incidents_created = db.scalar(select(func.count()).select_from(Incident)) or 0
    incidents_merged = db.scalar(
        select(func.count()).select_from(Incident).where(Incident.status == IncidentStatus.merged)
    ) or 0
    root_cause_candidates = sum(
        len(rc or []) for rc in db.scalars(select(Incident.root_cause_candidates)).all()
    )

    failures = _stored_metric(db, "correlation_failures")
    correlation_failures = int(failures.count) if failures else 0

    duration = _stored_metric(db, "correlation_duration_seconds")
    average_processing_time = round(duration.value / duration.count, 4) if duration and duration.count else 0.0

    return {
        "events_received": events_received,
        "events_normalized": events_normalized,
        "events_deduplicated": events_deduplicated,
        "correlations_created": correlations_created,
        "incidents_created": incidents_created,
        "incidents_merged": incidents_merged,
        "root_cause_candidates": root_cause_candidates,
        "correlation_failures": correlation_failures,
        "average_processing_time": average_processing_time,
    }
