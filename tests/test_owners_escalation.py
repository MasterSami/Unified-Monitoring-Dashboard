"""Owner resolution + alert Escalate mailto."""

from __future__ import annotations

from app.owners import dynatrace_owner, extract_email, owner_from_tags, resolve_owner


def test_dynatrace_owner_resolution_chain():
    # 1) exact-key owner tag with a value (+ email extraction)
    ent = {"tags": [{"key": "Owner", "value": "NOC Team <noc@corp.com>"}]}
    assert dynatrace_owner(ent) == ("NOC Team <noc@corp.com>", "noc@corp.com")
    # a near-miss key ("teamwork") must NOT match "team"
    ent = {"tags": [{"key": "teamwork", "value": "x"}],
           "managementZones": [{"name": "Payments"}]}
    assert dynatrace_owner(ent) == ("Payments", None)  # falls through to MZ
    # 2) stringRepresentation "owner:someone"
    ent = {"tags": [{"key": "owner", "stringRepresentation": "owner:alice"}]}
    assert dynatrace_owner(ent) == ("alice", None)
    # 4) host group as the weakest proxy
    ent = {"properties": {"hostGroupName": "PROD-DB"}}
    assert dynatrace_owner(ent) == ("PROD-DB", None)
    # nothing at all
    assert dynatrace_owner({}) == (None, None)


def test_affected_host_prefers_host_entity():
    from app.collectors.dynatrace import DynatraceCollector as DC

    # A service problem that also lists the underlying host -> host wins.
    prob = {"affectedEntities": [
        {"entityId": {"id": "SERVICE-1", "type": "SERVICE"}, "name": "IIS"},
        {"entityId": {"id": "HOST-ABC", "type": "HOST"}, "name": "web-prod-1"},
    ]}
    assert DC._affected_host(prob) == ("HOST-ABC", "web-prod-1")
    # Pure service problem -> no host id, keep the service name.
    prob = {"affectedEntities": [{"entityId": {"id": "SERVICE-9", "type": "SERVICE"}, "name": "maximo"}]}
    assert DC._affected_host(prob) == (None, "maximo")


def test_dynatrace_service_alert_resolves_owner_via_host_id(client):
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models import Alert, Host, HostStatus, SourcePlatform
    from app.routers.pages import _active_alerts

    now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        # Host is keyed by its Dynatrace entity id; it has an owner.
        db.add(Host(external_id="HOST-DWH14", source_platform=SourcePlatform.dynatrace,
                    source_instance="Dynatrace-IT", hostname="DWH-CVM-PROD14",
                    ip="10.19.99.15", status=HostStatus.up,
                    owner="Payments", owner_email="pay@corp.com"))
        # Alert's host_hostname is a decorated/service string, but it carries the
        # HOST entity id -> owner still resolves.
        db.add(Alert(external_id="P-77", source_platform=SourcePlatform.dynatrace,
                     source_instance="Dynatrace-IT",
                     host_hostname="host: DWH-CVM-PROD14 IP: 10.19.99.15 OS: RHEL",
                     host_external_id="HOST-DWH14", severity_int=4,
                     severity_label="Availability", title="Host unavailable",
                     started_at=now, resolved=False))
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        rows, *_ = _active_alerts(db, "P-77", 1, state="active")
        # search by title since hostname is decorated
        rows, *_ = _active_alerts(db, "Host unavailable", 1, state="active")
        a = next(r for r in rows if r.external_id == "P-77")
        assert a.owner == "Payments" and a.owner_email == "pay@corp.com"
        assert a.escalate_href.startswith("mailto:pay%40corp.com?")
    finally:
        db.close()


def test_nnmi_alerts_never_get_escalate_link(client):
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models import Alert, Host, HostStatus, SourcePlatform
    from app.routers.pages import _active_alerts

    now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        # An NNMi host that even has an owner recorded...
        db.add(Host(external_id="nn1", source_platform=SourcePlatform.nnmi,
                    source_instance="NNMi-76", hostname="TEWAF01", ip="10.1.1.1",
                    status=HostStatus.down, owner="net-team", owner_email="net@corp.com"))
        db.add(Alert(external_id="nn-a", source_platform=SourcePlatform.nnmi,
                     source_instance="NNMi-76", host_hostname="TEWAF01", severity_int=5,
                     severity_label="Critical", title="AddressNotResponding",
                     started_at=now, resolved=False))
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        rows, *_ = _active_alerts(db, "TEWAF01", 1, state="active")
        assert rows and rows[0].source_platform == SourcePlatform.nnmi
        assert rows[0].escalate_href is None  # NNMi never escalatable
    finally:
        db.close()


def test_extract_email_and_tag_resolution():
    assert extract_email("Ahmed <ahmed.hassan@corp.com>") == "ahmed.hassan@corp.com"
    assert extract_email("no email here") is None

    # Zabbix tag shape (tag/value); email is pulled out of the value.
    owner, email = resolve_owner(
        [{"tag": "Owner", "value": "Ahmed Hassan <ahmed@corp.com>"}], None
    )
    assert owner == "Ahmed Hassan <ahmed@corp.com>"
    assert email == "ahmed@corp.com"

    # Dynatrace tag shape (key/value), key merely contains "owner".
    owner, email = owner_from_tags(
        [{"context": "CONTEXTLESS", "key": "Server Owner", "value": "team@corp.com"}]
    )
    assert owner == "team@corp.com" and email == "team@corp.com"


def test_inventory_fallback_and_no_owner():
    owner, email = resolve_owner([], {"contact": "noc@corp.com"})
    assert owner == "noc@corp.com" and email == "noc@corp.com"
    # alias is a weaker fallback and carries no email
    assert resolve_owner([], {"alias": "srv01"}) == ("srv01", None)
    assert resolve_owner([{"tag": "env", "value": "prod"}], {}) == (None, None)


def test_escalation_attached_only_to_active_alerts(client):
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models import Alert, Host, HostStatus, SourcePlatform
    from app.routers.pages import _active_alerts

    now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(Host(external_id="h-esc", source_platform=SourcePlatform.zabbix,
                    source_instance="ESC", hostname="db-esc", ip="10.5.5.5",
                    status=HostStatus.up, owner="Ahmed", owner_email="ahmed@corp.com"))
        db.add(Alert(external_id="a-esc-open", source_platform=SourcePlatform.zabbix,
                     source_instance="ESC", host_hostname="db-esc", severity_int=4,
                     severity_label="High", title="CPU high", started_at=now,
                     resolved=False))
        db.add(Alert(external_id="a-esc-done", source_platform=SourcePlatform.zabbix,
                     source_instance="ESC", host_hostname="db-esc", severity_int=4,
                     severity_label="High", title="was high", started_at=now,
                     resolved=True))
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        rows, *_ = _active_alerts(db, "db-esc", 1, state="all")
        by_id = {a.external_id: a for a in rows}
        active = by_id["a-esc-open"]
        done = by_id["a-esc-done"]
        # owner resolved from the host for both
        assert active.owner == "Ahmed" and active.owner_email == "ahmed@corp.com"
        # only the active alert gets a mailto, and it targets the owner
        assert active.escalate_href.startswith("mailto:ahmed%40corp.com?")
        assert "FYA" in active.escalate_href
        assert done.escalate_href is None
    finally:
        db.close()
