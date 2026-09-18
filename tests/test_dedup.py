"""Correlation Phase 2: deterministic event fingerprinting + deduplication.

Covers app.fingerprint (pure normalization/fingerprint functions), app.dedup
and its wiring into app.normalizer's upsert paths, and the /logical-events
read API. No AI/ML anywhere — every normalization rule is a fixed keyword
pattern and every fingerprint a deterministic composite key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.fingerprint import compute_fingerprint, normalize_problem_type
from app.models import Alert, HostStatus, LogicalEvent, LogicalEventStatus, SourcePlatform
from app.normalizer import upsert_alerts, upsert_hosts, upsert_resolved_alerts

INST = "DEDUP-TEST"
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


def _make_host(db, *, platform, instance, external_id, hostname, ip):
    upsert_hosts(
        db, platform,
        [{"external_id": external_id, "hostname": hostname, "ip": ip, "status": HostStatus.up}],
        instance,
    )
    db.commit()


class TestNormalizeProblemType:
    def test_dynamic_values_normalize_the_same(self):
        """The task's own example: 91% / 94% / 97% -> one stable category."""
        a = normalize_problem_type(title="CPU utilization is 91%")
        b = normalize_problem_type(title="CPU utilization is 94%")
        c = normalize_problem_type(title="CPU utilization is 97%")
        assert a == b == c == "CPU_HIGH"

    def test_unrelated_titles_do_not_collapse_into_one_bucket(self):
        """Neither matches a known category, so both hit the fallback path —
        which must NOT act as a universal catch-all."""
        a = normalize_problem_type(title="Queue backlog is growing")
        b = normalize_problem_type(title="Certificate is expiring soon")
        assert a != b
        assert a not in ("UNKNOWN", "")
        assert b not in ("UNKNOWN", "")

    def test_generic_source_category_falls_back_to_the_more_specific_title(self):
        """Dynatrace's own problem_type can be a generic bucket (CUSTOM_ALERT);
        the title usually carries the real signal."""
        result = normalize_problem_type(
            problem_type="CUSTOM_ALERT", title="Memory saturation on host X",
        )
        assert result == "MEMORY_HIGH"

    def test_disk_and_availability_are_distinct_categories(self):
        assert normalize_problem_type(title="Free disk space is low") == "DISK_HIGH"
        assert normalize_problem_type(title="Host is unreachable") == "AVAILABILITY_DOWN"


class TestComputeFingerprint:
    def test_no_entity_means_no_fingerprint(self):
        """Never guess an anchor to group on — see app/fingerprint.py."""
        assert compute_fingerprint(entity_id=None, normalized_problem_type="CPU_HIGH") is None

    def test_same_entity_and_problem_type_share_a_fingerprint(self):
        a = compute_fingerprint(entity_id=1, normalized_problem_type="CPU_HIGH")
        b = compute_fingerprint(entity_id=1, normalized_problem_type="CPU_HIGH")
        assert a == b

    def test_different_entities_never_share_a_fingerprint(self):
        a = compute_fingerprint(entity_id=1, normalized_problem_type="CPU_HIGH")
        b = compute_fingerprint(entity_id=2, normalized_problem_type="CPU_HIGH")
        assert a != b

    def test_different_problem_types_on_the_same_entity_never_share_a_fingerprint(self):
        a = compute_fingerprint(entity_id=1, normalized_problem_type="CPU_HIGH")
        b = compute_fingerprint(entity_id=1, normalized_problem_type="DISK_HIGH")
        assert a != b

    def test_irrelevant_fields_are_never_included(self):
        """'Use only relevant fields': no api_id given -> no api component at
        all, so two api-less alerts on the same entity/problem still match."""
        a = compute_fingerprint(entity_id=1, normalized_problem_type="ERROR")
        b = compute_fingerprint(entity_id=1, normalized_problem_type="ERROR", api_id=None)
        assert a == b
        c = compute_fingerprint(entity_id=1, normalized_problem_type="ERROR", api_id=99)
        assert c != a


class TestRepeatedIdenticalAlerts:
    """Acceptance criteria: CPU HIGH x10 -> one logical event, Occurrences:
    10, every original event retained."""

    def test_ten_cpu_episodes_become_one_logical_event_with_ten_occurrences(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-cpu10", hostname="CPUHOST", ip="10.40.0.1",
            )
            for i in range(10):
                upsert_resolved_alerts(
                    db, SourcePlatform.zabbix,
                    [{
                        "external_id": f"cpu10-ev-{i}",
                        "host_hostname": "CPUHOST", "host_external_id": "h-cpu10",
                        "severity_int": 3, "severity_label": "Average",
                        "title": f"CPU utilization is {90 + i}%",
                        "started_at": NOW - timedelta(minutes=i),
                    }],
                    INST,
                )
            db.commit()

            rows = db.scalars(
                select(Alert).where(
                    Alert.host_external_id == "h-cpu10", Alert.source_instance == INST,
                )
            ).all()
            assert len(rows) == 10  # nothing deleted or merged
            fingerprints = {r.fingerprint for r in rows}
            assert len(fingerprints) == 1  # all ten share one fingerprint

            le = db.scalars(
                select(LogicalEvent).where(LogicalEvent.fingerprint == fingerprints.pop())
            ).one()
            assert le.occurrence_count == 10
            assert le.normalized_problem_type == "CPU_HIGH"
            # Resolved-history episodes: every occurrence is resolved=True, so
            # the group is RESOLVED — RESOLVED outranks DEDUPLICATED whenever
            # every occurrence has closed (see app.dedup._compute_status).
            # TestMultipleSources below covers a DEDUPLICATED group directly
            # (2+ occurrences, at least one still active).
            assert le.status == LogicalEventStatus.resolved
        finally:
            db.close()


class TestDuplicateSourceDelivery:
    """Idempotency: (source, source_instance, source_event_id) must never
    create a duplicate logical event, or a duplicate Alert row."""

    def test_repolling_the_identical_alert_creates_nothing_new(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-dup", hostname="DUPHOST", ip="10.40.1.1",
            )
            item = {
                "external_id": "dup-alert-1", "host_hostname": "DUPHOST",
                "host_external_id": "h-dup", "severity_int": 3,
                "severity_label": "Average", "title": "CPU utilization is 88%",
                "started_at": NOW,
            }
            upsert_alerts(db, SourcePlatform.zabbix, [item], INST)
            db.commit()
            row = db.scalars(
                select(Alert).where(Alert.external_id == "dup-alert-1", Alert.source_instance == INST)
            ).one()
            first_le_id = row.logical_event_id
            assert first_le_id is not None

            # The exact same source event, delivered again (a re-poll).
            upsert_alerts(db, SourcePlatform.zabbix, [dict(item)], INST)
            db.commit()

            count = db.scalar(
                select(Alert.id).where(
                    Alert.external_id == "dup-alert-1", Alert.source_instance == INST,
                )
            )
            rows = db.scalars(
                select(Alert).where(Alert.external_id == "dup-alert-1", Alert.source_instance == INST)
            ).all()
            assert len(rows) == 1  # no duplicate Alert row
            assert rows[0].logical_event_id == first_le_id

            le = db.get(LogicalEvent, first_le_id)
            assert le.occurrence_count == 1  # not double-counted
            assert le.status == LogicalEventStatus.open
        finally:
            db.close()


class TestSameHostDifferentProblems:
    def test_cpu_and_disk_on_the_same_host_stay_separate(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-multi", hostname="MULTIPROB", ip="10.40.2.1",
            )
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [
                    {"external_id": "mp-cpu", "host_hostname": "MULTIPROB",
                     "host_external_id": "h-multi", "severity_int": 3,
                     "severity_label": "Average", "title": "CPU utilization is 91%",
                     "started_at": NOW},
                    {"external_id": "mp-disk", "host_hostname": "MULTIPROB",
                     "host_external_id": "h-multi", "severity_int": 3,
                     "severity_label": "Average", "title": "Free disk space is low",
                     "started_at": NOW},
                ],
                INST,
            )
            db.commit()
            cpu = db.scalars(select(Alert).where(Alert.external_id == "mp-cpu")).one()
            disk = db.scalars(select(Alert).where(Alert.external_id == "mp-disk")).one()
            assert cpu.entity_id == disk.entity_id  # same host
            assert cpu.fingerprint != disk.fingerprint
            assert cpu.logical_event_id != disk.logical_event_id
        finally:
            db.close()


class TestSameProblemDifferentHosts:
    """Acceptance criteria: APP01 CPU HIGH / APP02 CPU HIGH must stay separate.

    Uses P2APP01/P2APP02 rather than the literal APP01/APP02 from the task
    text — entity resolution's hostname/alias matching is global, not scoped
    per test file, and test_entity_resolution.py's own acceptance-criteria
    test already registers APP01 as an alias of a DIFFERENT entity; reusing
    that exact string here would collide with it instead of demonstrating
    anything about this phase.
    """

    def test_cpu_high_on_two_different_hosts_stays_separate(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-p2app01", hostname="P2APP01", ip="10.40.3.1",
            )
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-p2app02", hostname="P2APP02", ip="10.40.3.2",
            )
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [
                    {"external_id": "app01-cpu", "host_hostname": "P2APP01",
                     "host_external_id": "h-p2app01", "severity_int": 3,
                     "severity_label": "Average", "title": "CPU utilization is 93%",
                     "started_at": NOW},
                    {"external_id": "app02-cpu", "host_hostname": "P2APP02",
                     "host_external_id": "h-p2app02", "severity_int": 3,
                     "severity_label": "Average", "title": "CPU utilization is 95%",
                     "started_at": NOW},
                ],
                INST,
            )
            db.commit()
            a = db.scalars(select(Alert).where(Alert.external_id == "app01-cpu")).one()
            b = db.scalars(select(Alert).where(Alert.external_id == "app02-cpu")).one()
            assert a.normalized_problem_type == b.normalized_problem_type == "CPU_HIGH"
            assert a.entity_id != b.entity_id
            assert a.fingerprint != b.fingerprint
            assert a.logical_event_id != b.logical_event_id
        finally:
            db.close()


class TestMultipleSources:
    """Zabbix CPU HIGH + Dynatrace's equivalent problem on the SAME resolved
    entity associate to one logical condition — while each occurrence keeps
    its own source, source_event_id, and original_severity."""

    def test_two_platforms_reporting_the_same_condition_share_one_logical_event(self, client):
        db = SessionLocal()
        try:
            upsert_hosts(
                db, SourcePlatform.zabbix,
                [{"external_id": "ms-zbx", "hostname": "SHARED01", "ip": "10.40.4.1",
                  "status": HostStatus.up}],
                INST,
            )
            db.commit()
            upsert_hosts(
                db, SourcePlatform.dynatrace,
                [{"external_id": "ms-dt", "hostname": "shared-01", "ip": "10.40.4.1",
                  "status": HostStatus.up}],
                INST,
            )
            db.commit()

            upsert_alerts(
                db, SourcePlatform.zabbix,
                [{"external_id": "ms-zbx-alert", "host_hostname": "SHARED01",
                  "host_external_id": "ms-zbx", "severity_int": 3,
                  "severity_label": "Average", "title": "CPU utilization is 90%",
                  "started_at": NOW}],
                INST,
            )
            db.commit()
            upsert_alerts(
                db, SourcePlatform.dynatrace,
                [{"external_id": "ms-dt-alert", "host_hostname": "shared-01",
                  "host_external_id": "ms-dt", "severity_int": 3,
                  "severity_label": "Medium", "title": "CPU saturation detected",
                  "started_at": NOW}],
                INST,
            )
            db.commit()

            zbx = db.scalars(select(Alert).where(Alert.external_id == "ms-zbx-alert")).one()
            dt = db.scalars(select(Alert).where(Alert.external_id == "ms-dt-alert")).one()
            assert zbx.entity_id == dt.entity_id  # Phase 1: same IP -> same entity
            assert zbx.logical_event_id == dt.logical_event_id

            le = db.get(LogicalEvent, zbx.logical_event_id)
            assert le.occurrence_count == 2
            assert le.sources == ["dynatrace", "zabbix"]
            # Both occurrences are still active -> DEDUPLICATED (2+ linked,
            # at least one unresolved) — see app.dedup._compute_status.
            assert le.status == LogicalEventStatus.deduplicated

            # Each occurrence still carries its OWN source identity, exactly
            # as originally reported — nothing here overwrote it.
            assert zbx.source_platform == SourcePlatform.zabbix
            assert zbx.external_id == "ms-zbx-alert"
            assert zbx.original_severity == "Average"
            assert dt.source_platform == SourcePlatform.dynatrace
            assert dt.external_id == "ms-dt-alert"
            assert dt.original_severity == "Medium"
        finally:
            db.close()


class TestResolvedThenReopened:
    def test_resolving_then_a_new_occurrence_reopens_the_group(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="h-reopen", hostname="REOPENHOST", ip="10.40.5.1",
            )
            item = {
                "external_id": "reopen-ev-1", "host_hostname": "REOPENHOST",
                "host_external_id": "h-reopen", "severity_int": 3,
                "severity_label": "Average", "title": "CPU utilization is 92%",
                "started_at": NOW,
            }
            upsert_alerts(db, SourcePlatform.zabbix, [item], INST)
            db.commit()
            row = db.scalars(
                select(Alert).where(Alert.external_id == "reopen-ev-1", Alert.source_instance == INST)
            ).one()
            le_id = row.logical_event_id
            le = db.get(LogicalEvent, le_id)
            assert le.status == LogicalEventStatus.open

            # Next poll: the trigger cleared (absent from this run) -> reconciled resolved.
            upsert_alerts(db, SourcePlatform.zabbix, [], INST)
            db.commit()
            db.refresh(le)
            assert le.status == LogicalEventStatus.resolved

            # The same condition fires again, as a new episode.
            item2 = {**item, "external_id": "reopen-ev-2", "started_at": NOW + timedelta(hours=2)}
            upsert_alerts(db, SourcePlatform.zabbix, [item2], INST)
            db.commit()
            db.refresh(le)
            assert le.status == LogicalEventStatus.reopened
            assert le.occurrence_count == 2
        finally:
            db.close()


class TestLogicalEventsAPI:
    def test_list_detail_and_occurrences_endpoints(self, client):
        db = SessionLocal()
        try:
            _make_host(
                db, platform=SourcePlatform.zabbix, instance="API-DEDUP-TEST",
                external_id="api-h1", hostname="APIDEDUP1", ip="10.40.6.1",
            )
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [{"external_id": "api-dedup-ev1", "host_hostname": "APIDEDUP1",
                  "host_external_id": "api-h1", "severity_int": 3,
                  "severity_label": "Average", "title": "CPU utilization is 90%",
                  "started_at": NOW}],
                "API-DEDUP-TEST",
            )
            db.commit()
            row = db.scalars(select(Alert).where(Alert.external_id == "api-dedup-ev1")).one()
            le_id = row.logical_event_id
        finally:
            db.close()

        r = client.get("/api/v1/logical-events", params={"entity_id": row.entity_id})
        assert r.status_code == 200
        data = r.json()
        assert any(e["id"] == le_id for e in data)

        r2 = client.get(f"/api/v1/logical-events/{le_id}")
        assert r2.status_code == 200
        detail = r2.json()
        assert detail["normalized_problem_type"] == "CPU_HIGH"
        assert detail["occurrence_count"] == 1

        r3 = client.get(f"/api/v1/logical-events/{le_id}/occurrences")
        assert r3.status_code == 200
        occs = r3.json()
        assert len(occs) == 1
        assert occs[0]["external_id"] == "api-dedup-ev1"
        assert occs[0]["original_severity"] == "Average"

    def test_unknown_logical_event_is_404(self, client):
        assert client.get("/api/v1/logical-events/99999999").status_code == 404
        assert client.get("/api/v1/logical-events/99999999/occurrences").status_code == 404
