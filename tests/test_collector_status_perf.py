"""Collector health status is derived in a fixed, small number of queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import CollectorRun, RunStatus
from app.scheduler import get_collector_statuses

BASE = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)


def _seed(db, instance, nrun, last_failed):
    for i in range(nrun):
        st = RunStatus.failed if (last_failed and i == nrun - 1) else RunStatus.success
        db.add(CollectorRun(
            platform="sitescope", instance=instance, started_at=BASE + timedelta(minutes=i),
            finished_at=BASE + timedelta(minutes=i), status=st,
            items_collected=i, hosts_collected=i, alerts_collected=i,
        ))


def test_status_is_batched_and_correct(client):
    db = SessionLocal()
    try:
        _seed(db, "SiteScope-PERF-A", 5, True)   # latest run failed, has earlier success
        _seed(db, "SiteScope-PERF-B", 3, False)  # all success
        db.commit()
    finally:
        db.close()

    n = {"q": 0}

    def _count(*_a, **_k):
        n["q"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    db = SessionLocal()
    try:
        statuses = get_collector_statuses(db, get_settings())
    finally:
        db.close()
        event.remove(engine, "before_cursor_execute", _count)

    by = {s.instance: s for s in statuses}
    a = by["SiteScope-PERF-A"]
    assert a.status == "failed"                 # latest run wins
    assert a.last_success_at is not None        # earlier success still tracked
    assert by["SiteScope-PERF-B"].status == "success"
    # Two batched queries regardless of how many instances exist.
    assert n["q"] <= 3
