"""APScheduler wiring and per-collector run orchestration.

Exposes a small :class:`CollectorService` that owns the collector instances and
knows how to run one or all of them, plus helpers to start/stop the background
scheduler. Status is derived from persisted :class:`CollectorRun` rows so it
survives restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors import build_collector, build_collectors
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import CollectorRun, RunStatus
from app.schemas import CollectorStatus

logger = logging.getLogger("scheduler")

_JOB_ID = "poll_all_collectors"


class CollectorService:
    """Owns collector instances and runs them against fresh DB sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collectors = build_collectors(settings)

    def run_one(self, name: str) -> bool:
        """Run a single collector by name. Returns True if it ran.

        Uses the enabled instance when present, otherwise builds an ad-hoc one
        so a manual trigger works even for a disabled platform.
        """
        collector = self.collectors.get(name) or build_collector(
            name, self.settings
        )
        if collector is None:
            logger.warning("unknown collector requested: %s", name)
            return False
        db: Session = SessionLocal()
        try:
            collector.run(db)
        finally:
            db.close()
        return True

    def run_all(self) -> None:
        """Run every enabled collector once. Failures are contained per run."""
        logger.info("polling all collectors: %s", list(self.collectors))
        for name in self.collectors:
            self.run_one(name)

    def has_data(self) -> bool:
        """Return True if any collector run has ever been recorded."""
        db: Session = SessionLocal()
        try:
            return db.scalar(select(CollectorRun.id).limit(1)) is not None
        finally:
            db.close()


# --- Status derivation ------------------------------------------------------


def get_collector_statuses(db: Session, settings: Settings) -> list[CollectorStatus]:
    """Derive current health for every known collector from run history."""
    from app.collectors import COLLECTOR_CLASSES

    enabled = set(settings.enabled_collectors_list)
    statuses: list[CollectorStatus] = []

    for name in COLLECTOR_CLASSES:
        last_run = db.scalar(
            select(CollectorRun)
            .where(CollectorRun.platform == name)
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        )
        last_success = db.scalar(
            select(CollectorRun)
            .where(
                CollectorRun.platform == name,
                CollectorRun.status == RunStatus.success,
            )
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        )

        is_enabled = name in enabled
        if not is_enabled:
            status = "disabled"
        elif last_run is None:
            status = "never"
        else:
            status = last_run.status.value

        last_run_at: datetime | None = last_run.started_at if last_run else None
        statuses.append(
            CollectorStatus(
                platform=name,
                enabled=is_enabled,
                last_run_at=last_run_at,
                last_success_at=last_success.finished_at if last_success else None,
                status=status,
                items_collected=last_run.items_collected if last_run else 0,
                error_message=last_run.error_message
                if last_run and last_run.status == RunStatus.failed
                else None,
                notes=last_success.error_message
                if last_success and last_success.error_message
                else None,
            )
        )
    return statuses


# --- Scheduler lifecycle ----------------------------------------------------

_scheduler: BackgroundScheduler | None = None
_service: CollectorService | None = None


def get_service() -> CollectorService:
    """Return the process-wide :class:`CollectorService`, building it lazily."""
    global _service
    if _service is None:
        _service = CollectorService(get_settings())
    return _service


def start_scheduler(settings: Settings) -> BackgroundScheduler:
    """Start the background polling scheduler and return it."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    service = get_service()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        service.run_all,
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id=_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "scheduler started; polling every %d minute(s)",
        settings.poll_interval_minutes,
    )
    return scheduler


def shutdown_scheduler() -> None:
    """Stop the background scheduler if running."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler stopped")
