"""Correlation Phase 3: deterministic topology / dependency graph.

Covers app.dependency_graph (traversal engine), app.topology_sync
(Dynatrace/NNMi adapters reading the existing TopologyNode/TopologyEdge
tables), and the /topology/* API. No AI/ML — every traversal is a plain BFS
with fixed, documented direction rules (see app/dependency_graph.py).
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.dependency_graph import direct_relationships, find_path, record_relationship, traverse
from app.models import (
    CanonicalEntity,
    EntityRelationship,
    EntityType,
    LogicalEvent,
    LogicalEventStatus,
    RelationshipType,
    SourcePlatform,
    TopologyEdge,
    TopologyNode,
    TopologySource,
)
from app.topology_sync import sync_dynatrace_relationships, sync_nnmi_relationships

INST = "TOPO-TEST"


def _entity(db, entity_type: EntityType, name: str) -> int:
    e = CanonicalEntity(entity_type=entity_type, canonical_name=name)
    db.add(e)
    db.flush()
    return e.id


def _rel(
    db, from_id: int, to_id: int, rel_type: RelationshipType,
    *, source: TopologySource = TopologySource.manual, evidence: str | None = None,
) -> EntityRelationship:
    return record_relationship(
        db, source=source, relationship_type=rel_type,
        from_entity_id=from_id, to_entity_id=to_id, evidence=evidence,
    )


class TestHostDependency:
    def test_application_hosted_on_host_reachable_both_directions(self, client):
        db = SessionLocal()
        try:
            app_id = _entity(db, EntityType.application, "OrderApp")
            host_id = _entity(db, EntityType.host, "APPSRV01")
            _rel(db, app_id, host_id, RelationshipType.hosted_on)
            db.commit()

            deps = traverse(db, app_id, direction="upstream")
            assert {n.entity_id for n in deps.nodes} == {host_id}

            impact = traverse(db, host_id, direction="downstream")
            assert {n.entity_id for n in impact.nodes} == {app_id}
        finally:
            db.close()


class TestServiceDependency:
    def test_service_depends_on_service(self, client):
        db = SessionLocal()
        try:
            a = _entity(db, EntityType.service, "AuthService")
            b = _entity(db, EntityType.service, "TokenService")
            _rel(db, a, b, RelationshipType.depends_on)
            db.commit()
            deps = traverse(db, a, direction="upstream")
            assert [n.entity_id for n in deps.nodes] == [b]
            assert deps.nodes[0].relationship_type == "depends_on"
        finally:
            db.close()


class TestAPIToService:
    def test_api_calls_service(self, client):
        db = SessionLocal()
        try:
            api_id = _entity(db, EntityType.api, "OrdersAPI")
            svc_id = _entity(db, EntityType.service, "OrdersService")
            _rel(db, api_id, svc_id, RelationshipType.calls)
            db.commit()
            deps = traverse(db, api_id, direction="upstream")
            assert [n.entity_id for n in deps.nodes] == [svc_id]
        finally:
            db.close()


class TestServiceToDB:
    def test_database_failure_impacts_the_service_that_depends_on_it(self, client):
        db = SessionLocal()
        try:
            svc_id = _entity(db, EntityType.service, "OrdersService2")
            db_id = _entity(db, EntityType.database, "OrdersDB")
            _rel(db, svc_id, db_id, RelationshipType.depends_on)
            db.commit()
            impact = traverse(db, db_id, direction="downstream")
            assert {n.entity_id for n in impact.nodes} == {svc_id}
        finally:
            db.close()


class TestApplicationToDB:
    def test_which_applications_depend_on_this_database(self, client):
        db = SessionLocal()
        try:
            app_id = _entity(db, EntityType.application, "ReportingApp")
            other_app_id = _entity(db, EntityType.application, "UnrelatedApp")
            db_id = _entity(db, EntityType.database, "ReportingDB")
            _rel(db, app_id, db_id, RelationshipType.depends_on)
            db.commit()
            impact = traverse(db, db_id, direction="downstream")
            applications = [n for n in impact.nodes if n.entity_type == "application"]
            assert {n.entity_id for n in applications} == {app_id}
            assert other_app_id not in {n.entity_id for n in impact.nodes}
        finally:
            db.close()


class TestNetworkToHost:
    def test_nnmi_l2_connection_syncs_into_connects_to_between_devices(self, client):
        db = SessionLocal()
        try:
            db.add(TopologyNode(
                source_platform=SourcePlatform.nnmi, source_instance=INST,
                external_id="sw1", kind="device", name="CoreSwitch1",
            ))
            db.add(TopologyNode(
                source_platform=SourcePlatform.nnmi, source_instance=INST,
                external_id="sw2", kind="device", name="EdgeSwitch1",
            ))
            db.add(TopologyEdge(
                source_platform=SourcePlatform.nnmi, source_instance=INST,
                external_id="l2-1", kind="l2", from_external_id="sw1",
                to_external_id="sw2", label="Gi0/1 - Gi0/2",
            ))
            db.commit()

            written = sync_nnmi_relationships(db)
            db.commit()
            assert written == 1

            rel = db.scalars(
                select(EntityRelationship).where(EntityRelationship.source == TopologySource.monitoring)
            ).one()
            assert rel.relationship_type == RelationshipType.connects_to
            from_e = db.get(CanonicalEntity, rel.from_entity_id)
            to_e = db.get(CanonicalEntity, rel.to_entity_id)
            assert {from_e.canonical_name, to_e.canonical_name} == {"CoreSwitch1", "EdgeSwitch1"}
            assert from_e.entity_type == EntityType.network_device

            # Re-running the sync must not duplicate the relationship.
            written_again = sync_nnmi_relationships(db)
            db.commit()
            assert written_again == 1
            count = db.scalar(
                select(EntityRelationship.id).where(EntityRelationship.source == TopologySource.monitoring)
            )
            all_rows = db.scalars(
                select(EntityRelationship).where(EntityRelationship.source == TopologySource.monitoring)
            ).all()
            assert len(all_rows) == 1
        finally:
            db.close()


class TestDynatraceSync:
    def test_dynatrace_service_call_graph_syncs_into_calls_relationships(self, client):
        db = SessionLocal()
        try:
            db.add(TopologyNode(
                source_platform=SourcePlatform.dynatrace, source_instance=INST,
                external_id="SERVICE-A", kind="service", name="Checkout Service",
            ))
            db.add(TopologyNode(
                source_platform=SourcePlatform.dynatrace, source_instance=INST,
                external_id="SERVICE-B", kind="service", name="Payment Service",
            ))
            db.add(TopologyEdge(
                # app/topology.py writes Dynatrace call paths with kind="path".
                source_platform=SourcePlatform.dynatrace, source_instance=INST,
                external_id="call-1", kind="path", from_external_id="SERVICE-A",
                to_external_id="SERVICE-B", label="HTTP",
            ))
            db.commit()

            written = sync_dynatrace_relationships(db)
            db.commit()
            assert written == 1
            rel = db.scalars(
                select(EntityRelationship).where(EntityRelationship.source == TopologySource.dynatrace)
            ).one()
            assert rel.relationship_type == RelationshipType.calls
            assert "Checkout Service" in rel.evidence and "Payment Service" in rel.evidence
        finally:
            db.close()


class TestMultiHopAndAcceptanceCriteria:
    """Acceptance criteria: Customer API -> CALLS -> CustomerService ->
    DEPENDS_ON -> CustomerDB must show Customer API as indirectly dependent
    on CustomerDB.
    """

    def test_customer_api_is_indirectly_dependent_on_customer_db(self, client):
        db = SessionLocal()
        try:
            api_id = _entity(db, EntityType.api, "Customer API")
            svc_id = _entity(db, EntityType.service, "CustomerService")
            db_id = _entity(db, EntityType.database, "CustomerDB")
            _rel(db, api_id, svc_id, RelationshipType.calls,
                 evidence="Customer API calls CustomerService")
            _rel(db, svc_id, db_id, RelationshipType.depends_on,
                 evidence="CustomerService depends on CustomerDB")
            db.commit()

            deps = traverse(db, api_id, direction="upstream", max_depth=5)
            reached = {n.entity_id: n.depth for n in deps.nodes}
            assert reached[svc_id] == 1
            assert reached[db_id] == 2  # indirect — two hops away

            path = find_path(db, api_id, db_id)
            assert path is not None
            assert [h.entity_id for h in path] == [svc_id, db_id]

            impact = traverse(db, db_id, direction="downstream")
            assert {n.entity_id for n in impact.nodes} == {svc_id, api_id}
        finally:
            db.close()

    def test_full_chain_from_business_service_down_to_a_host(self, client):
        db = SessionLocal()
        try:
            biz = _entity(db, EntityType.business_service, "Customer360")
            api_id = _entity(db, EntityType.api, "C360-API")
            svc_id = _entity(db, EntityType.service, "C360-Service")
            db_id = _entity(db, EntityType.database, "C360-DB")
            host_id = _entity(db, EntityType.host, "C360-DBHOST")
            _rel(db, api_id, biz, RelationshipType.part_of)
            _rel(db, api_id, svc_id, RelationshipType.calls)
            _rel(db, svc_id, db_id, RelationshipType.depends_on)
            _rel(db, db_id, host_id, RelationshipType.hosted_on)
            db.commit()

            impact = traverse(db, host_id, direction="downstream", max_depth=10)
            assert {n.entity_id for n in impact.nodes} == {db_id, svc_id, api_id, biz}
        finally:
            db.close()


class TestCycles:
    def test_a_cycle_terminates_and_is_flagged_not_infinite(self, client):
        db = SessionLocal()
        try:
            a = _entity(db, EntityType.service, "CycleA")
            b = _entity(db, EntityType.service, "CycleB")
            c = _entity(db, EntityType.service, "CycleC")
            _rel(db, a, b, RelationshipType.depends_on)
            _rel(db, b, c, RelationshipType.depends_on)
            _rel(db, c, a, RelationshipType.depends_on)  # cycles back to the root
            db.commit()

            result = traverse(db, a, direction="upstream", max_depth=10)
            assert result.cycle_detected is True
            assert {n.entity_id for n in result.nodes} == {b, c}
            # the root is never listed as its own dependency
            assert a not in {n.entity_id for n in result.nodes}
        finally:
            db.close()

    def test_max_depth_bounds_a_long_chain(self, client):
        db = SessionLocal()
        try:
            ids = [_entity(db, EntityType.service, f"ChainNode{i}") for i in range(8)]
            for i in range(len(ids) - 1):
                _rel(db, ids[i], ids[i + 1], RelationshipType.depends_on)
            db.commit()

            result = traverse(db, ids[0], direction="upstream", max_depth=3)
            assert len(result.nodes) == 3
            assert result.truncated is True
            assert result.cycle_detected is False
        finally:
            db.close()


class TestMissingTopology:
    def test_entity_with_no_relationships_returns_empty_not_an_error(self, client):
        db = SessionLocal()
        try:
            lonely_id = _entity(db, EntityType.service, "NoRelationsService")
            db.commit()

            deps = traverse(db, lonely_id, direction="upstream")
            assert deps.nodes == []
            assert deps.truncated is False
            assert deps.cycle_detected is False

            rels = direct_relationships(db, lonely_id)
            assert rels == {"outgoing": [], "incoming": []}

            path = find_path(db, lonely_id, 987654321)
            assert path is None
        finally:
            db.close()


class TestMultipleSourcesNeverBlindlyMerged:
    def test_two_sources_claiming_the_same_pair_both_persist(self, client):
        db = SessionLocal()
        try:
            svc_id = _entity(db, EntityType.service, "MultiSourceSvc")
            db_id = _entity(db, EntityType.database, "MultiSourceDB")
            _rel(db, svc_id, db_id, RelationshipType.depends_on,
                 source=TopologySource.cmdb, evidence="CMDB dependency")
            _rel(db, svc_id, db_id, RelationshipType.depends_on,
                 source=TopologySource.dynatrace, evidence="Service A called Service B")
            db.commit()

            rows = db.scalars(
                select(EntityRelationship).where(
                    EntityRelationship.from_entity_id == svc_id,
                    EntityRelationship.to_entity_id == db_id,
                )
            ).all()
            assert len(rows) == 2
            assert {r.source for r in rows} == {TopologySource.cmdb, TopologySource.dynatrace}
        finally:
            db.close()

    def test_re_recording_the_same_source_claim_updates_in_place(self, client):
        db = SessionLocal()
        try:
            svc_id = _entity(db, EntityType.service, "IdemSvc")
            db_id = _entity(db, EntityType.database, "IdemDB")
            _rel(db, svc_id, db_id, RelationshipType.depends_on,
                 source=TopologySource.manual, evidence="first note")
            db.commit()
            _rel(db, svc_id, db_id, RelationshipType.depends_on,
                 source=TopologySource.manual, evidence="updated note")
            db.commit()

            rows = db.scalars(
                select(EntityRelationship).where(
                    EntityRelationship.from_entity_id == svc_id,
                    EntityRelationship.to_entity_id == db_id,
                    EntityRelationship.source == TopologySource.manual,
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].evidence == "updated note"
        finally:
            db.close()


class TestActiveIssueEnrichment:
    """The dependencies/impact API marks nodes with a live Phase 2
    LogicalEvent as having an active issue — real data, not a guess, and
    what the UI uses to tell a healthy node from an affected one."""

    def test_downstream_impact_flags_the_entity_with_an_open_logical_event(self, client):
        db = SessionLocal()
        try:
            svc_id = _entity(db, EntityType.service, "IssueSvc")
            db_id = _entity(db, EntityType.database, "IssueDB")
            _rel(db, svc_id, db_id, RelationshipType.depends_on)
            db.add(LogicalEvent(
                fingerprint=f"entity:{svc_id}|problem:CPU_HIGH",
                entity_id=svc_id, normalized_problem_type="CPU_HIGH",
                status=LogicalEventStatus.open, occurrence_count=1,
            ))
            db.commit()
        finally:
            db.close()

        r = client.get(f"/api/v1/topology/impact/{db_id}")
        assert r.status_code == 200
        data = r.json()
        node = next(n for n in data["nodes"] if n["entity_id"] == svc_id)
        assert node["has_active_issue"] is True

        r2 = client.get(f"/api/v1/topology/dependencies/{svc_id}")
        assert r2.status_code == 200
        assert r2.json()["root_has_active_issue"] is True


class TestAPI:
    def test_create_list_entity_dependencies_impact_and_path(self, client):
        db = SessionLocal()
        try:
            api_id = _entity(db, EntityType.api, "API-TOPO-1")
            svc_id = _entity(db, EntityType.service, "SVC-TOPO-1")
            db_id = _entity(db, EntityType.database, "DB-TOPO-1")
            db.commit()
        finally:
            db.close()

        r = client.post("/api/v1/topology/relationships", json={
            "source": "manual", "relationship_type": "calls",
            "from_entity_id": api_id, "to_entity_id": svc_id,
            "evidence": "API calls service",
        })
        assert r.status_code == 201
        r2 = client.post("/api/v1/topology/relationships", json={
            "source": "manual", "relationship_type": "depends_on",
            "from_entity_id": svc_id, "to_entity_id": db_id,
            "evidence": "service needs the DB",
        })
        assert r2.status_code == 201

        r3 = client.get("/api/v1/topology/relationships", params={"entity_id": svc_id})
        assert r3.status_code == 200
        assert len(r3.json()) == 2

        r4 = client.get(f"/api/v1/topology/entities/{svc_id}")
        assert r4.status_code == 200
        detail = r4.json()
        assert any(n["entity_id"] == db_id for n in detail["outgoing"])
        assert any(n["entity_id"] == api_id for n in detail["incoming"])

        r5 = client.get(f"/api/v1/topology/dependencies/{api_id}")
        assert r5.status_code == 200
        dep = r5.json()
        assert {n["entity_id"] for n in dep["nodes"]} == {svc_id, db_id}

        r6 = client.get(f"/api/v1/topology/impact/{db_id}")
        assert r6.status_code == 200
        imp = r6.json()
        assert {n["entity_id"] for n in imp["nodes"]} == {svc_id, api_id}

        r7 = client.get(
            "/api/v1/topology/path",
            params={"from_entity_id": api_id, "to_entity_id": db_id},
        )
        assert r7.status_code == 200
        path = r7.json()
        assert path["found"] is True
        assert [h["entity_id"] for h in path["hops"]] == [svc_id, db_id]

    def test_relationship_to_self_is_rejected(self, client):
        db = SessionLocal()
        try:
            eid = _entity(db, EntityType.service, "SelfRelSvc")
            db.commit()
        finally:
            db.close()
        r = client.post("/api/v1/topology/relationships", json={
            "source": "manual", "relationship_type": "depends_on",
            "from_entity_id": eid, "to_entity_id": eid,
        })
        assert r.status_code == 422

    def test_unknown_entity_is_404_everywhere(self, client):
        assert client.get("/api/v1/topology/entities/99999999").status_code == 404
        assert client.get("/api/v1/topology/dependencies/99999999").status_code == 404
        assert client.get("/api/v1/topology/impact/99999999").status_code == 404
        assert client.get(
            "/api/v1/topology/path",
            params={"from_entity_id": 99999999, "to_entity_id": 1},
        ).status_code == 404

    def test_sync_endpoint_runs_both_adapters(self, client):
        r = client.post("/api/v1/topology/sync")
        assert r.status_code == 200
        body = r.json()
        assert "dynatrace" in body and "monitoring" in body


class TestPage:
    def test_disabled_by_default(self, client):
        r = client.get("/dependency-graph")
        assert r.status_code == 200
        assert "turned off" in r.text
        assert "ENABLE_DEPENDENCY_GRAPH" in r.text

    def test_enabled_with_no_root_shows_search_prompt(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_dependency_graph", True)
        r = client.get("/dependency-graph")
        assert r.status_code == 200
        assert "turned off" not in r.text
        assert "dg-search" in r.text

    def test_enabled_with_a_root_entity_shows_its_name(self, client, monkeypatch):
        from app.config import get_settings

        db = SessionLocal()
        try:
            eid = _entity(db, EntityType.service, "PageTestEntity")
            db.commit()
        finally:
            db.close()

        monkeypatch.setattr(get_settings(), "enable_dependency_graph", True)
        r = client.get(f"/dependency-graph?entity_id={eid}")
        assert r.status_code == 200
        assert "PageTestEntity" in r.text
