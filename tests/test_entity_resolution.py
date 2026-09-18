"""Correlation Phase 1: canonical event model + deterministic entity resolution.

Covers the resolution engine directly (app.entity_resolution), its wiring
into the normalize/upsert paths (app.normalizer, app.sitescope_ingest), and
the read API (/events, /entities, /entity-mappings). No AI/ML anywhere here
— every assertion is about an exact, deterministic match method.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal
from app.entity_resolution import create_manual_mapping, resolve_host_entity
from app.models import (
    Alert,
    CanonicalEntity,
    EntityIP,
    EntityType,
    Host,
    HostStatus,
    ResolutionMethod,
    SourcePlatform,
)
from app.normalizer import upsert_alerts, upsert_hosts

INST = "ER-TEST"
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


class TestExactIPMatch:
    def test_two_sources_sharing_an_ip_resolve_to_one_entity(self, client):
        db = SessionLocal()
        try:
            zabbix = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="ip-z-app01", hostname="APP01", ip="10.19.30.196",
            )
            db.commit()
            assert zabbix.method == ResolutionMethod.new_entity

            dynatrace = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance=INST,
                external_id="ip-dt-app-prod-01", hostname="app-prod-01",
                ip="10.19.30.196",
            )
            db.commit()
            assert dynatrace.method == ResolutionMethod.ip_exact_match
            assert dynatrace.entity_id == zabbix.entity_id
        finally:
            db.close()

    def test_secondary_ip_in_ip_all_also_matches(self, client):
        db = SessionLocal()
        try:
            base = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance=INST,
                external_id="ip-dt-multi", hostname="multi-nic",
                ip="10.20.0.1", ip_all="10.20.0.1, 10.20.0.2",
            )
            db.commit()
            other = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="ip-z-multi", hostname="multi-nic-zbx",
                ip="10.20.0.2",  # only the *secondary* address
            )
            assert other.method == ResolutionMethod.ip_exact_match
            assert other.entity_id == base.entity_id
        finally:
            db.close()


class TestHostnameMatch:
    def test_matching_hostname_alone_resolves(self, client):
        db = SessionLocal()
        try:
            base = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="hn-z-web-a", hostname="web-a", ip="10.1.1.1",
            )
            db.commit()
            # A different platform, no IP given at all — only the hostname ties
            # the two together.
            other = resolve_host_entity(
                db, platform=SourcePlatform.nnmi, instance=INST,
                external_id="hn-nn-web-a", hostname="web-a",
            )
            assert other.method == ResolutionMethod.hostname_exact_match
            assert other.entity_id == base.entity_id
        finally:
            db.close()

    def test_hostname_match_is_checked_only_after_ip_finds_nothing(self, client):
        """IP is checked first (priority 3) and hostname only falls back to
        matching (priority 5) when IP evidence is absent or inconclusive —
        not because IP "disagreed", but because a bare hostname is never
        trusted ahead of a stronger signal that could have settled it.
        """
        db = SessionLocal()
        try:
            first = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="hn-fallback-a", hostname="shared-name", ip="10.3.3.3",
            )
            db.commit()
            # A second host with the same hostname but an IP nothing has
            # claimed yet — IP finds no match, so resolution falls back to
            # the hostname it does share with `first`.
            second = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance=INST,
                external_id="hn-fallback-b", hostname="shared-name", ip="10.3.3.4",
            )
            db.commit()
            assert second.method == ResolutionMethod.hostname_exact_match
            assert second.entity_id == first.entity_id
        finally:
            db.close()


class TestFQDNMatch:
    def test_fqdn_ties_two_different_hostnames_together(self, client):
        db = SessionLocal()
        try:
            base = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="fq-z-db1", hostname="DB1", fqdn="db1.corp.example.com",
            )
            db.commit()
            other = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance=INST,
                external_id="fq-dt-db1", hostname="db-one",  # deliberately different
                fqdn="db1.corp.example.com",
            )
            assert other.method == ResolutionMethod.fqdn_exact_match
            assert other.entity_id == base.entity_id
        finally:
            db.close()


class TestManualMapping:
    def test_manual_mapping_resolves_with_no_shared_ip_or_hostname(self, client):
        db = SessionLocal()
        try:
            base = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="mm-z-core1", hostname="CORE1", ip="10.2.2.2",
            )
            db.commit()
            create_manual_mapping(
                db, entity_id=base.entity_id,
                source_platform=SourcePlatform.nnmi, source_instance=INST,
                source_identifier="CORE1_TE", note="linked by ops, no shared IP",
            )
            db.commit()

            result = resolve_host_entity(
                db, platform=SourcePlatform.nnmi, instance=INST,
                external_id="CORE1_TE", hostname="CORE1_TE",  # unrelated string
            )
            assert result.method == ResolutionMethod.manual_mapping
            assert result.entity_id == base.entity_id
        finally:
            db.close()

    def test_manual_mapping_outranks_a_weaker_automatic_match(self, client):
        """Priority 1 beats priority 5: a wrong hostname-only guess never wins
        once an admin has stated the answer explicitly."""
        db = SessionLocal()
        try:
            decoy = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="mm2-decoy", hostname="AMBIG-NAME",
            )
            correct = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance=INST,
                external_id="mm2-correct", hostname="the-real-one",
            )
            db.commit()
            create_manual_mapping(
                db, entity_id=correct.entity_id,
                source_platform=SourcePlatform.nnmi, source_instance=INST,
                source_identifier="AMBIG-NAME",
            )
            db.commit()

            result = resolve_host_entity(
                db, platform=SourcePlatform.nnmi, instance=INST,
                external_id="AMBIG-NAME", hostname="AMBIG-NAME",
            )
            assert result.method == ResolutionMethod.manual_mapping
            assert result.entity_id == correct.entity_id
            assert result.entity_id != decoy.entity_id
        finally:
            db.close()


class TestConflict:
    def test_conflicting_ip_ownership_refuses_to_guess(self, client):
        db = SessionLocal()
        try:
            e1 = CanonicalEntity(entity_type=EntityType.host, canonical_name="conflict-one")
            e2 = CanonicalEntity(entity_type=EntityType.host, canonical_name="conflict-two")
            db.add_all([e1, e2])
            db.flush()
            db.add(EntityIP(entity_id=e1.id, ip="10.9.9.9"))
            db.add(EntityIP(entity_id=e2.id, ip="10.9.9.8"))
            db.commit()

            result = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="cf-ambiguous", hostname="ambiguous-host",
                ip="10.9.9.9", ip_all="10.9.9.9, 10.9.9.8",
            )
            assert result.method == ResolutionMethod.conflict
            assert result.entity_id is None
            assert result.confidence == 0.0
        finally:
            db.close()


class TestUnknownEntity:
    def test_first_sighting_creates_a_new_entity(self, client):
        db = SessionLocal()
        try:
            result = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance=INST,
                external_id="new-never-seen", hostname="brand-new-host", ip="10.7.7.7",
            )
            db.commit()
            assert result.method == ResolutionMethod.new_entity
            assert result.created_new is True
            assert result.confidence == 1.0
            entity = db.get(CanonicalEntity, result.entity_id)
            assert entity is not None
            assert entity.canonical_name == "brand-new-host"
        finally:
            db.close()


class TestDuplicateSourceEvent:
    def test_repolling_the_same_host_does_not_create_a_second_entity(self, client):
        db = SessionLocal()
        try:
            upsert_hosts(
                db, SourcePlatform.zabbix,
                [{"external_id": "dup-h1", "hostname": "DUP1", "ip": "10.6.6.6",
                  "status": HostStatus.up}],
                INST,
            )
            db.commit()
            row = db.scalars(
                select(Host).where(Host.external_id == "dup-h1", Host.source_instance == INST)
            ).one()
            first_entity_id = row.entity_id
            assert first_entity_id is not None

            # Same source event again, identity unchanged.
            upsert_hosts(
                db, SourcePlatform.zabbix,
                [{"external_id": "dup-h1", "hostname": "DUP1", "ip": "10.6.6.6",
                  "status": HostStatus.up}],
                INST,
            )
            db.commit()
            db.refresh(row)
            assert row.entity_id == first_entity_id
            owners = db.scalar(
                select(func.count()).select_from(EntityIP).where(EntityIP.ip == "10.6.6.6")
            )
            assert owners == 1  # still exactly one entity claims this address

        finally:
            db.close()

    def test_duplicate_alert_event_keeps_one_row_and_freezes_the_original_values(
        self, client
    ):
        db = SessionLocal()
        try:
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [{"external_id": "dup-alert-1", "host_hostname": "H1",
                  "severity_int": 2, "severity_label": "Warning",
                  "title": "disk filling up", "started_at": NOW}],
                INST,
            )
            db.commit()
            row = db.scalars(
                select(Alert).where(
                    Alert.external_id == "dup-alert-1", Alert.source_instance == INST
                )
            ).one()
            assert row.original_severity == "Warning"
            assert row.original_description == "disk filling up"
            assert row.status == "open"

            # The SAME event re-fires (duplicate source event) with an escalated
            # severity and an edited title — the source's current view moves on.
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [{"external_id": "dup-alert-1", "host_hostname": "H1",
                  "severity_int": 5, "severity_label": "Disaster",
                  "title": "disk full", "started_at": NOW}],
                INST,
            )
            db.commit()
            db.refresh(row)

            assert row.severity_label == "Disaster"       # current view: updated
            assert row.original_severity == "Warning"      # first-seen: frozen
            assert row.original_description == "disk filling up"

            count = db.scalar(
                select(func.count()).select_from(Alert).where(
                    Alert.external_id == "dup-alert-1", Alert.source_instance == INST
                )
            )
            assert count == 1  # no duplicate row
        finally:
            db.close()


class TestMultipleSourcesSameEntity:
    """Acceptance criteria: Zabbix APP01 + Dynatrace app-prod-01 (same IP) +
    NNMi APP01_TE (a differently-styled name, tied in by an explicit mapping
    since it shares neither IP nor hostname with the other two — exactly the
    case ``ResolutionMethod.manual_mapping`` exists for) must resolve to one
    canonical entity.
    """

    def test_zabbix_dynatrace_nnmi_resolve_to_one_entity(self, client):
        db = SessionLocal()
        try:
            zabbix = resolve_host_entity(
                db, platform=SourcePlatform.zabbix, instance="ACCEPT-ZBX",
                external_id="zbx-10245", hostname="APP01", ip="10.19.30.196",
            )
            db.commit()

            dynatrace = resolve_host_entity(
                db, platform=SourcePlatform.dynatrace, instance="ACCEPT-DT",
                external_id="HOST-ABC123", hostname="app-prod-01",
                ip="10.19.30.196",
            )
            db.commit()
            assert dynatrace.method == ResolutionMethod.ip_exact_match

            create_manual_mapping(
                db, entity_id=zabbix.entity_id,
                source_platform=SourcePlatform.nnmi, source_instance="ACCEPT-NNMI",
                source_identifier="APP01_TE",
                note="NNMi's element-name suffix does not match either other "
                     "source's hostname or IP; declared explicitly.",
            )
            db.commit()

            nnmi = resolve_host_entity(
                db, platform=SourcePlatform.nnmi, instance="ACCEPT-NNMI",
                external_id="APP01_TE", hostname="APP01_TE",
            )

            assert zabbix.entity_id == dynatrace.entity_id == nnmi.entity_id
            assert nnmi.method == ResolutionMethod.manual_mapping

            entity = db.get(CanonicalEntity, zabbix.entity_id)
            assert entity.entity_type == EntityType.host
        finally:
            db.close()


class TestEventsAPI:
    def test_events_endpoint_returns_canonical_fields(self, client):
        db = SessionLocal()
        try:
            upsert_alerts(
                db, SourcePlatform.zabbix,
                [{"external_id": "api-ev-1", "host_hostname": "APIH1",
                  "severity_int": 3, "severity_label": "Average",
                  "title": "cpu high", "started_at": NOW}],
                "API-EVENTS-TEST",
            )
            db.commit()
        finally:
            db.close()

        r = client.get(
            "/api/v1/events",
            params={"platform": "zabbix", "instance": "API-EVENTS-TEST"},
        )
        assert r.status_code == 200
        data = r.json()
        ev = next(e for e in data if e["external_id"] == "api-ev-1")
        assert ev["original_severity"] == "Average"
        assert ev["original_description"] == "cpu high"
        assert ev["status"] == "open"
        assert ev["event_type"] == "zabbix_trigger"
        assert ev["source_platform"] == "zabbix"


class TestEntitiesAPI:
    def test_entity_detail_lists_ips_and_source_rows(self, client):
        db = SessionLocal()
        try:
            upsert_hosts(
                db, SourcePlatform.zabbix,
                [{"external_id": "api-h1", "hostname": "APIHOST1",
                  "ip": "10.50.0.1", "status": HostStatus.up}],
                "API-ENTITIES-TEST",
            )
            db.commit()
            host = db.scalars(
                select(Host).where(
                    Host.external_id == "api-h1",
                    Host.source_instance == "API-ENTITIES-TEST",
                )
            ).one()
            entity_id = host.entity_id
        finally:
            db.close()

        r = client.get(f"/api/v1/entities/{entity_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["entity_id"] == entity_id
        assert "10.50.0.1" in data["ips"]
        assert any(
            s["source_instance"] == "API-ENTITIES-TEST" and s["external_id"] == "api-h1"
            for s in data["source_entities"]
        )

        r2 = client.get("/api/v1/entity-mappings", params={"entity_id": entity_id})
        assert r2.status_code == 200
        rows = r2.json()
        assert any(row["source_identifier"] == "api-h1" and not row["is_manual"] for row in rows)

    def test_unknown_entity_is_404(self, client):
        r = client.get("/api/v1/entities/99999999")
        assert r.status_code == 404

    def test_create_manual_mapping_via_api_then_resolves(self, client):
        r = client.post(
            "/api/v1/entity-mappings",
            json={
                "source_platform": "nnmi",
                "source_instance": "API-MANUAL-TEST",
                "source_identifier": "MANUAL01",
                "entity_type": "host",
                "canonical_name": "manual01-entity",
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["is_manual"] is True
        entity_id = data["entity_id"]

        db = SessionLocal()
        try:
            result = resolve_host_entity(
                db, platform=SourcePlatform.nnmi, instance="API-MANUAL-TEST",
                external_id="MANUAL01", hostname="MANUAL01",
            )
            assert result.entity_id == entity_id
            assert result.method == ResolutionMethod.manual_mapping
        finally:
            db.close()
