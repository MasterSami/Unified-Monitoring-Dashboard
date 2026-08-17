"""Resolved-alert history backfill (upsert_resolved_alerts)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Alert, SourcePlatform
from app.normalizer import upsert_alerts, upsert_resolved_alerts

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
INST = "ZBX-HIST-TEST"


def test_backfill_inserts_history_and_dedups_reconciled_episode(client):
    db = SessionLocal()
    try:
        # Live run 1: trigger 111 active. Live run 2: gone -> reconciled resolved.
        upsert_alerts(db, SourcePlatform.zabbix, [{
            "external_id": "111", "host_hostname": "db01", "severity_int": 4,
            "severity_label": "High", "title": "CPU high", "started_at": NOW,
        }], INST)
        upsert_alerts(db, SourcePlatform.zabbix, [], INST)
        db.commit()

        # Backfill: the SAME episode as an event (matches trigger + started_at)
        # plus an older, pre-deploy episode that only exists in history.
        inserted = upsert_resolved_alerts(db, SourcePlatform.zabbix, [
            {
                "external_id": "ev-9001", "match_external_id": "111",
                "host_hostname": "db01", "severity_int": 4,
                "severity_label": "High", "title": "CPU high",
                "started_at": NOW,
            },
            {
                "external_id": "ev-8000", "match_external_id": "111",
                "host_hostname": "db01", "severity_int": 4,
                "severity_label": "High", "title": "CPU high",
                "started_at": datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
            },
        ], INST)
        db.commit()
        assert inserted == 1  # only the pre-deploy episode is new

        rows = db.scalars(
            select(Alert).where(Alert.source_instance == INST)
        ).all()
        assert len(rows) == 2
        assert all(r.resolved for r in rows)

        # Idempotent: running the same backfill again adds nothing.
        assert upsert_resolved_alerts(db, SourcePlatform.zabbix, [
            {
                "external_id": "ev-8000", "match_external_id": "111",
                "host_hostname": "db01", "severity_int": 4,
                "severity_label": "High", "title": "CPU high",
                "started_at": datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
            },
        ], INST) == 0
        db.commit()
    finally:
        db.close()


def test_backfill_marks_previously_active_row_resolved(client):
    inst = "DT-HIST-TEST"
    db = SessionLocal()
    try:
        # A Dynatrace problem that is currently active…
        upsert_alerts(db, SourcePlatform.dynatrace, [{
            "external_id": "P-42", "host_hostname": "web01", "severity_int": 3,
            "severity_label": "ERROR", "title": "Response time", "started_at": NOW,
        }], inst)
        db.commit()
        # …later comes back from the CLOSED-problems history (same problemId).
        inserted = upsert_resolved_alerts(db, SourcePlatform.dynatrace, [{
            "external_id": "P-42", "host_hostname": "web01", "severity_int": 3,
            "severity_label": "ERROR", "title": "Response time", "started_at": NOW,
        }], inst)
        db.commit()
        assert inserted == 0  # updated in place, no duplicate
        row = db.scalars(
            select(Alert).where(Alert.source_instance == inst)
        ).one()
        assert row.resolved is True
    finally:
        db.close()
