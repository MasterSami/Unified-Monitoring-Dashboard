"""APScheduler wiring and per-instance run orchestration.

Owns one collector per configured server (across all platforms), knows how to
run one or all of them, and derives health from persisted
:class:`~app.models.CollectorRun` rows so status survives restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors import BaseCollector, build_collectors
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import CollectorRun, RunStatus
from app.schemas import CollectorStatus
from app.servers import load_servers
from app.topology import run_topology

logger = logging.getLogger("scheduler")

_JOB_ID = "poll_all_collectors"
_TOPOLOGY_JOB_ID = "poll_topology"


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
                hosts_collected=last_run.hosts_collected if last_run else 0,
                alerts_collected=last_run.alerts_collected if last_run else 0,
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

    # Push-based collectors (e.g. the SiteScope forwarder) aren't in the server
    # inventory — derive their health from the run rows they record on ingest,
    # so a stale/dead forwarder still shows up in the UI.
    configured = {cfg.name for cfg in load_servers(settings)}
    push_instances = db.execute(
        select(CollectorRun.instance)
        .where(CollectorRun.platform == "sitescope")
        .group_by(CollectorRun.instance)
    ).scalars().all()
    for instance in push_instances:
        if instance in configured:
            continue
        last_run = db.scalar(
            select(CollectorRun)
            .where(
                CollectorRun.platform == "sitescope",
                CollectorRun.instance == instance,
            )
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        )
        if last_run is None:
            continue
        statuses.append(
            CollectorStatus(
                platform="sitescope",
                instance=instance,
                enabled=True,
                last_run_at=last_run.started_at,
                last_success_at=last_run.finished_at,
                status=last_run.status.value,
                items_collected=last_run.items_collected,
                hosts_collected=last_run.hosts_collected,
                alerts_collected=last_run.alerts_collected,
                error_message=None,
                notes="push (forwarder)",
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


_SITESCOPE_DEMO_JOB_ID = "sitescope_demo_load"


def _load_sitescope_file(instance: str, path: str) -> None:
    """Load one redacted SiteScope .tsv through the shared ingest path.

    Read-only on the file; records a CollectorRun so the instance shows in the
    collector list. Contained — one bad file never affects the others or the
    scheduler.
    """
    from app.sitescope_ingest import ingest_lines  # lazy: avoids import cycle

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\r\n") for ln in fh if ln.strip()]
    except OSError as exc:
        logger.warning("sitescope file unreadable (%s -> %s): %s", instance, path, exc)
        return

    db: Session = SessionLocal()
    try:
        started = datetime.now(timezone.utc)
        counts = ingest_lines(db, instance, lines)
        db.add(
            CollectorRun(
                platform="sitescope",
                instance=instance,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                status=RunStatus.success,
                items_collected=counts.events,
                hosts_collected=counts.hosts,
                alerts_collected=counts.events,
            )
        )
        db.commit()
        logger.info(
            "sitescope: loaded %d line(s) for %s from %s -> %d event(s), %d host(s)",
            len(lines), instance, path, counts.events, counts.hosts,
        )
    except Exception:  # pragma: no cover - must never crash the scheduler
        db.rollback()
        logger.exception("sitescope load failed for %s", instance)
    finally:
        db.close()


def _run_sitescope_demo_job() -> None:
    """Auto-load every configured SiteScope .tsv (one or many instances).

    Enabled when SITESCOPE_DEMO_FILE / SITESCOPE_DEMO_FILES is set. Lets the whole
    SiteScope scenario run on a laptop (hosts + alerts, auto-refreshing) with no
    forwarder — the ONLY difference from production is env config. If the files
    are kept fresh on disk, the data stays live.
    """
    for instance, path in get_settings().sitescope_demo_map:
        _load_sitescope_file(instance, path)


def _run_topology_job() -> None:
    """Scheduler entry point: rebuild every instance's topology graph."""
    run_topology(get_settings())


def run_topology_now() -> None:
    """Rebuild topology synchronously (used by the manual API trigger)."""
    run_topology(get_settings())


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
    # Topology (NNMi network map + Dynatrace service map) is gated behind
    # ENABLE_TOPOLOGY. It changes slowly, so it polls on a much longer interval
    # than the host/alert collectors — but still fires once on startup.
    if settings.enable_topology:
        topo_minutes = max(settings.poll_interval_minutes * 6, 30)
        scheduler.add_job(
            _run_topology_job,
            trigger="interval",
            minutes=topo_minutes,
            id=_TOPOLOGY_JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        logger.info("topology collection enabled; polling every %d minute(s)", topo_minutes)

    # Optional local SiteScope demo: if SITESCOPE_DEMO_FILE is set, auto-load
    # that redacted .tsv on startup and refresh it every poll interval, so
    # SiteScope appears as a full platform with no forwarder / no server access.
    demo_map = settings.sitescope_demo_map
    if demo_map:
        scheduler.add_job(
            _run_sitescope_demo_job,
            trigger="interval",
            minutes=settings.poll_interval_minutes,
            id=_SITESCOPE_DEMO_JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        logger.info(
            "sitescope auto-load enabled for %d instance(s): %s (every %d min)",
            len(demo_map), ", ".join(i for i, _ in demo_map),
            settings.poll_interval_minutes,
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
