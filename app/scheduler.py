"""APScheduler wiring and per-instance run orchestration.

Owns one collector per configured server (across all platforms), knows how to
run one or all of them, and derives health from persisted
:class:`~app.models.CollectorRun` rows so status survives restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors import BaseCollector, build_collectors
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import CollectorRun, RunStatus
from app.schemas import CollectorStatus
from app.servers import load_servers

logger = logging.getLogger("scheduler")

_JOB_ID = "poll_all_collectors"


class CollectorService:
    """Owns collector instances and runs them against fresh DB sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        #: instance name -> collector
        self.collectors: dict[str, BaseCollector] = build_collectors(settings)

    def get(self, instance: str) -> BaseCollector | None:
        """Return the collector for an instance name, if configured."""
        return self.collectors.get(instance)

    def run_one(self, instance: str) -> bool:
        """Run a single instance's collector. Returns True if it ran."""
        collector = self.collectors.get(instance)
        if collector is None:
            logger.warning("unknown instance requested: %s", instance)
            return False
        db: Session = SessionLocal()
        try:
            collector.run(db)
        finally:
            db.close()
        return True

    def run_all(self) -> None:
        """Run every configured collector once. Failures are contained."""
        logger.info("polling %d instance(s): %s", len(self.collectors), list(self.collectors))
        for instance in list(self.collectors):
            self.run_one(instance)

    def has_data(self) -> bool:
        """Return True if any collector run has ever been recorded."""
        db: Session = SessionLocal()
        try:
            return db.scalar(select(CollectorRun.id).limit(1)) is not None
        finally:
            db.close()


# --- Status derivation ------------------------------------------------------


def get_collector_statuses(db: Session, settings: Settings) -> list[CollectorStatus]:
    """Derive current health for every configured instance from run history."""
    statuses: list[CollectorStatus] = []

    for cfg in load_servers(settings):
        last_run = db.scalar(
            select(CollectorRun)
            .where(CollectorRun.instance == cfg.name)
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        )
        last_success = db.scalar(
            select(CollectorRun)
            .where(
                CollectorRun.instance == cfg.name,
                CollectorRun.status == RunStatus.success,
            )
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        )

        status = "never" if last_run is None else last_run.status.value
        statuses.append(
            CollectorStatus(
                platform=cfg.platform,
                instance=cfg.name,
                enabled=True,
                last_run_at=last_run.started_at if last_run else None,
                last_success_at=last_success.finished_at if last_success else None,
                status=status,
                items_collected=last_run.items_collected if last_run else 0,
                error_message=last_run.error_message
                if last_run and last_run.status == RunStatus.failed
                else None,
                notes=last_success.error_message
                if last_success and last_success.error_message
                else None,
                test_mail=cfg.test_mail,
                check_proxies=cfg.check_proxies,
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
        # Fire once right away (in the background), then every interval.
        next_run_time=datetime.now(),
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
