"""APScheduler wiring and per-instance run orchestration.

Owns one collector per configured server (across all platforms), knows how to
run one or all of them, and derives health from persisted
:class:`~app.models.CollectorRun` rows so status survives restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select
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


def _latest_runs_by_instance(
    db: Session, *, only_success: bool = False
) -> dict[str, CollectorRun]:
    """Return {instance -> latest CollectorRun} in ONE query.

    ``id`` is autoincrement, so the max id per instance is that instance's most
    recent run — cheaper and more portable than a per-row window function.
    """
    latest = select(
        CollectorRun.instance, func.max(CollectorRun.id).label("mid")
    )
    if only_success:
        latest = latest.where(CollectorRun.status == RunStatus.success)
    latest = latest.group_by(CollectorRun.instance).subquery()

    rows = db.scalars(
        select(CollectorRun).join(latest, CollectorRun.id == latest.c.mid)
    ).all()
    return {r.instance: r for r in rows}


def get_collector_statuses(db: Session, settings: Settings) -> list[CollectorStatus]:
    """Derive current health for every configured instance from run history.

    Two batched queries total (latest run + latest success per instance) instead
    of a pair of queries per instance — the sidebar health panel renders on every
    page and polls every 60s, so this stays cheap as instance count grows.
    """
    last_runs = _latest_runs_by_instance(db)
    last_success = _latest_runs_by_instance(db, only_success=True)
    statuses: list[CollectorStatus] = []

    configured: set[str] = set()
    for cfg in load_servers(settings):
        configured.add(cfg.name)
        last_run = last_runs.get(cfg.name)
        succ = last_success.get(cfg.name)
        status = "never" if last_run is None else last_run.status.value
        statuses.append(
            CollectorStatus(
                platform=cfg.platform,
                instance=cfg.name,
                enabled=True,
                last_run_at=last_run.started_at if last_run else None,
                last_success_at=succ.finished_at if succ else None,
                status=status,
                items_collected=last_run.items_collected if last_run else 0,
                hosts_collected=last_run.hosts_collected if last_run else 0,
                alerts_collected=last_run.alerts_collected if last_run else 0,
                error_message=last_run.error_message
                if last_run and last_run.status == RunStatus.failed
                else None,
                notes=succ.error_message if succ and succ.error_message else None,
                test_mail=cfg.test_mail,
                check_proxies=cfg.check_proxies,
            )
        )

    # Feeds that aren't in the server inventory — the SiteScope forwarder pushes
    # to us, the Digital View inventory is a file on disk — derive their health
    # from the run rows they record instead, so a dead forwarder or an
    # unreadable workbook still shows up in the UI. Already prefetched.
    #
    # This used to accept only "sitescope", which meant a Digital View failure
    # was written to the database and then never displayed anywhere.
    notes_by_platform = {
        "sitescope": "push (forwarder)",
        "digitalview": "asset inventory (file)",
    }
    for instance, last_run in last_runs.items():
        if instance in configured or last_run.platform not in notes_by_platform:
            continue
        succeeded = last_run.status == RunStatus.success
        statuses.append(
            CollectorStatus(
                platform=last_run.platform,
                instance=instance,
                enabled=True,
                last_run_at=last_run.started_at,
                last_success_at=(
                    last_success[instance].finished_at
                    if instance in last_success
                    else None
                ),
                status=last_run.status.value,
                items_collected=last_run.items_collected,
                hosts_collected=last_run.hosts_collected,
                alerts_collected=last_run.alerts_collected,
                # A failed run's message is the reason; a successful one's is a
                # note (e.g. the export date), which belongs in `notes`.
                error_message=None if succeeded else last_run.error_message,
                notes=(
                    last_run.error_message
                    if succeeded and last_run.error_message
                    else notes_by_platform[last_run.platform]
                ),
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


_DIGITALVIEW_JOB_ID = "digitalview_asset_load"
#: (mtime, size) of the workbook the last load read, so an unchanged file is
#: parsed once rather than on every tick — the export changes rarely.
_digitalview_stamp: tuple[float, int] | None = None


def _record_digitalview_failure(instance: str, reason: str) -> None:
    """Persist a failed run so the UI can show why the import produced nothing."""
    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.add(
            CollectorRun(
                platform="digitalview",
                instance=instance,
                started_at=now,
                finished_at=now,
                status=RunStatus.failed,
                items_collected=0,
                error_message=reason[:2048],
            )
        )
        db.commit()
    except Exception:  # pragma: no cover - reporting must never raise
        db.rollback()
    finally:
        db.close()


def _run_digitalview_job(force: bool = False) -> None:
    """Load the Huawei asset export, if it is new since the last load.

    Contained like every other scheduled job: a bad or missing workbook is
    logged and skipped, never allowed to stop the scheduler.
    """
    global _digitalview_stamp
    from pathlib import Path

    from app.digitalview_assets import load_into_db

    settings = get_settings()
    path = settings.digitalview_asset_file
    if not path:
        return

    instance = settings.digitalview_instance or "DigitalView"
    try:
        stat = Path(path).stat()
    except OSError as exc:
        # Record a FAILED run rather than only logging. A wrong path used to
        # look identical to "no assets" on the dashboard — the platform row
        # showed 0 with nothing to say why. Now the sidebar turns red and
        # carries the reason, like every other feed that cannot reach its source.
        logger.warning("digitalview asset file unreadable (%s): %s", path, exc)
        _record_digitalview_failure(instance, f"cannot read {path}: {exc}")
        return

    stamp = (stat.st_mtime, stat.st_size)
    if not force and _digitalview_stamp == stamp:
        return  # same export as last time; nothing to re-read
    _digitalview_stamp = stamp

    db: Session = SessionLocal()
    try:
        started = datetime.now(timezone.utc)
        inventory = load_into_db(db, instance, path)
        db.add(
            CollectorRun(
                platform="digitalview",
                instance=instance,
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                status=RunStatus.success,
                items_collected=inventory.count,
                hosts_collected=inventory.count,
                alerts_collected=0,
                error_message=(
                    "asset inventory (static export)"
                    + (
                        f", exported {inventory.exported_at:%Y-%m-%d}"
                        if inventory.exported_at
                        else ""
                    )
                ),
            )
        )
        db.commit()
        logger.info(
            "digitalview: loaded %d asset(s) for %s from %s",
            inventory.count, instance, path,
        )
    except Exception as exc:  # noqa: BLE001 — must never crash the scheduler
        db.rollback()
        _digitalview_stamp = None  # let the next tick retry
        logger.exception("digitalview asset load failed for %s", instance)
        _record_digitalview_failure(
            instance, f"could not read the workbook: {type(exc).__name__}: {exc}"
        )
    finally:
        db.close()


def _run_topology_job() -> None:
    """Scheduler entry point: rebuild every instance's topology graph."""
    run_topology(get_settings())


def run_topology_now() -> None:
    """Rebuild topology synchronously (used by the manual API trigger)."""
    run_topology(get_settings())


def _dispatch(job_id: str, func, **kwargs) -> bool:
    """Ask the scheduler to run ``func`` once, right now, in the background.

    Manual triggers ("Refresh now", "Rebuild topology") used to execute inline
    on the request thread, which meant one click held a FastAPI threadpool
    thread for as long as every collector took to answer — minutes, on a slow
    morning, and nothing stopped a handful of users from doing it at once.

    Handing the work to APScheduler instead makes the request return
    immediately and gives repeat clicks the right semantics for free: the
    polling job already carries ``max_instances=1`` and ``coalesce=True``, so a
    second click while a run is in flight is absorbed rather than doubled.

    Returns False when there is no scheduler (tests, or before startup), so the
    caller can fall back to running inline.
    """
    if _scheduler is None:
        return False
    _scheduler.add_job(
        func,
        trigger="date",
        run_date=datetime.now(),
        id=job_id,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        misfire_grace_time=None,
        kwargs=kwargs,
    )
    return True


def request_run_all() -> bool:
    """Queue a run of every collector. True if it was handed to the scheduler."""
    return _dispatch("manual_run_all", get_service().run_all)


def request_run_one(instance: str) -> bool:
    """Queue a run of one instance. True if it was handed to the scheduler."""
    return _dispatch(f"manual_run_{instance}", get_service().run_one, instance=instance)


def request_topology_run() -> bool:
    """Queue a topology rebuild. True if it was handed to the scheduler."""
    return _dispatch("manual_topology", run_topology_now)


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

    # Huawei asset inventory: a file on disk, re-read only when it changes.
    # Cheap enough to check every poll interval; the parse only runs on a new
    # export.
    if settings.digitalview_asset_file:
        scheduler.add_job(
            _run_digitalview_job,
            trigger="interval",
            minutes=settings.poll_interval_minutes,
            id=_DIGITALVIEW_JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        logger.info(
            "digitalview asset inventory enabled: %s (checked every %d min)",
            settings.digitalview_asset_file, settings.poll_interval_minutes,
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
