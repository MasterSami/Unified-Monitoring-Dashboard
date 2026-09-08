"""Service / group filter shared by Capacity, Agents and Alerts.

Operations teams know a server by the tool it belongs to, not by its IP. The
filter therefore has to work on the group name as the team says it — a
substring, case-insensitive — and, for alerts, has to reach the group through
the alert's host, because an alert carries no group of its own.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import Alert, Host, HostStatus, SourcePlatform
from app.routers.pages import (
    _active_alerts,
    _hosts_query,
    _service_clause,
    _service_names,
    _service_wanted,
)

INST = "SVC-FILTER-TEST"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _seed(db) -> None:
    """Three hosts across two services, and one alert on each host.

    Hosts:
      bill-db-01   Zabbix  groups "Billing, Linux servers"  (two groups, joined)
      crm-app-01   Zabbix  group  "CRM"
      bill-web-02  Dynatrace host group "Billing"
    Alerts:
      on bill-db-01  matched by hostname
      on bill-web-02 matched by host_external_id (title names a service)
      on crm-app-01  matched by hostname
    """
    db.add_all(
        [
            Host(
                hostname="bill-db-01", ip="10.1.1.1",
                source_platform=SourcePlatform.zabbix, source_instance=INST,
                external_id="svc-h1", status=HostStatus.up,
                group_name="Billing, Linux servers",
            ),
            Host(
                hostname="crm-app-01", ip="10.1.1.2",
                source_platform=SourcePlatform.zabbix, source_instance=INST,
                external_id="svc-h2", status=HostStatus.up, group_name="CRM",
            ),
            Host(
                hostname="bill-web-02", ip="10.1.1.3",
                source_platform=SourcePlatform.dynatrace, source_instance=INST,
                external_id="HOST-SVC3", status=HostStatus.up,
                group_name="Billing",
            ),
            Alert(
                external_id="svc-a1", source_platform=SourcePlatform.zabbix,
                source_instance=INST, host_hostname="bill-db-01",
                severity_int=4, severity_label="High", title="disk full",
                started_at=NOW, resolved=False,
            ),
            Alert(
                external_id="svc-a2", source_platform=SourcePlatform.dynatrace,
                source_instance=INST, host_hostname="billing-api",
                host_external_id="HOST-SVC3",
                severity_int=5, severity_label="Availability",
                title="service down", started_at=NOW, resolved=False,
            ),
            Alert(
                external_id="svc-a3", source_platform=SourcePlatform.zabbix,
                source_instance=INST, host_hostname="crm-app-01",
                severity_int=2, severity_label="Warning", title="cpu high",
                started_at=NOW, resolved=False,
            ),
        ]
    )
    db.commit()


def _hostnames(db, group: str | None) -> set[str]:
    rows, *_ = _hosts_query(
        db, None, "all", "all", "hostname", "asc", INST, 1, group
    )
    return {h.hostname for h in rows}


def test_service_wanted_treats_all_and_blank_as_no_filter():
    assert _service_wanted(None) is None
    assert _service_wanted("") is None
    assert _service_wanted("   ") is None
    assert _service_wanted("all") is None
    assert _service_wanted("ALL") is None
    assert _service_wanted("  Billing ") == "Billing"


def test_hosts_match_a_group_inside_zabbix_comma_joined_list(client):
    """"Billing" must find the host whose groups are "Billing, Linux servers".

    Equality on group_name would only ever match a host whose entire group
    list is the one name typed — which is never what the operator means.
    """
    db = SessionLocal()
    try:
        _seed(db)
        assert _hostnames(db, "Billing") == {"bill-db-01", "bill-web-02"}
        assert _hostnames(db, "billing") == {"bill-db-01", "bill-web-02"}   # case
        assert _hostnames(db, "bill") == {"bill-db-01", "bill-web-02"}      # partial
        assert _hostnames(db, "Linux") == {"bill-db-01"}                    # 2nd group
        assert _hostnames(db, "CRM") == {"crm-app-01"}
        assert _hostnames(db, "all") == {"bill-db-01", "bill-web-02", "crm-app-01"}
        assert _hostnames(db, "no-such-service") == set()
    finally:
        db.close()


def test_like_wildcards_in_the_typed_value_are_literal(client):
    """A group called CRM_Prod must not also match CRM-Prod through ``_``."""
    db = SessionLocal()
    try:
        db.add_all([
            Host(hostname="w1", source_platform=SourcePlatform.zabbix,
                 source_instance=INST + "-wc", external_id="wc1",
                 status=HostStatus.up, group_name="CRM_Prod"),
            Host(hostname="w2", source_platform=SourcePlatform.zabbix,
                 source_instance=INST + "-wc", external_id="wc2",
                 status=HostStatus.up, group_name="CRM-Prod"),
        ])
        db.commit()
        rows, *_ = _hosts_query(
            db, None, "all", "all", "hostname", "asc", INST + "-wc", 1, "CRM_Prod"
        )
        assert {h.hostname for h in rows} == {"w1"}
        rows, *_ = _hosts_query(
            db, None, "all", "all", "hostname", "asc", INST + "-wc", 1, "100%"
        )
        assert rows == []
    finally:
        db.close()


def test_alerts_filter_reaches_the_group_through_the_host(client):
    """An alert has no group; it inherits its host's, by external id or name."""
    db = SessionLocal()
    try:
        if not db.query(Host).filter_by(source_instance=INST).count():
            _seed(db)
        rows, total, *_ = _active_alerts(db, INST, 1, group="Billing")
        assert {a.external_id for a in rows} == {"svc-a1", "svc-a2"}
        assert total == 2
        # svc-a2's host_hostname ("billing-api") matches no host; it was found
        # through host_external_id — the Dynatrace case.
        rows, *_ = _active_alerts(db, INST, 1, group="CRM")
        assert {a.external_id for a in rows} == {"svc-a3"}
        rows, *_ = _active_alerts(db, INST, 1, group="")
        assert {a.external_id for a in rows} == {"svc-a1", "svc-a2", "svc-a3"}
    finally:
        db.close()


def test_service_names_split_comma_joined_groups_and_dedupe(client):
    db = SessionLocal()
    try:
        if not db.query(Host).filter_by(source_instance=INST).count():
            _seed(db)
        db.add(Host(hostname="dup-case", source_platform=SourcePlatform.nnmi,
                    source_instance=INST + "-nm", external_id="nm1",
                    status=HostStatus.up, group_name="billing"))
        db.commit()
        names = _service_names(db)
        # individual groups, not the joined combination
        assert "Billing" in names and "Linux servers" in names and "CRM" in names
        assert "Billing, Linux servers" not in names
        # case-insensitive dedupe keeps the first spelling seen, once
        assert sum(1 for n in names if n.lower() == "billing") == 1
        assert names == sorted(names, key=str.lower)
    finally:
        db.close()


def test_service_catalog_is_keyed_by_instance_with_its_platform(client):
    """The menu narrows to the platform tab / instance without a request."""
    from app.routers.pages import _service_catalog

    db = SessionLocal()
    try:
        if not db.query(Host).filter_by(source_instance=INST).count():
            _seed(db)
        db.add(Host(hostname="dt-only", source_platform=SourcePlatform.dynatrace,
                    source_instance=INST + "-dt", external_id="dt1",
                    status=HostStatus.up, group_name="Mediation"))
        db.commit()
        cat = _service_catalog(db)
    finally:
        db.close()

    # The seed instance carries both Zabbix and Dynatrace hosts; the catalog
    # is per (instance) and reports the platform the names came from.
    assert cat[INST + "-dt"] == {"platform": "dynatrace", "names": ["Mediation"]}
    assert "Mediation" not in cat[INST]["names"]
    # Comma-joined Zabbix groups are split; names are sorted and unique.
    assert "Billing" in cat[INST]["names"] and "Linux servers" in cat[INST]["names"]
    assert "Billing, Linux servers" not in cat[INST]["names"]
    assert cat[INST]["names"] == sorted(cat[INST]["names"], key=str.lower)
    assert list(cat) == sorted(cat, key=str.lower)


def test_pages_render_the_service_filter(client):
    """All three pages carry the same control with the catalog embedded."""
    for path in ("/capacity", "/agents", "/alerts"):
        html = client.get(path).text
        assert 'name="group"' in html, path
        assert "data-svcbox" in html, path
        assert "data-svc-catalog" in html, path
        assert '"platform": "' in html, path       # the JSON, not a datalist
        assert "<datalist" not in html, path
        assert "/static/svcbox.js" in html, path
        assert "Service / group" in html, path


def test_partials_and_exports_accept_the_filter(client):
    db = SessionLocal()
    try:
        if not db.query(Host).filter_by(source_instance=INST).count():
            _seed(db)
    finally:
        db.close()
    html = client.get("/partials/capacity", params={"group": "CRM", "instance": INST}).text
    assert "crm-app-01" in html and "bill-db-01" not in html
    html = client.get("/partials/agents", params={"group": "Billing", "instance": INST}).text
    assert "bill-db-01" in html and "crm-app-01" not in html
    html = client.get("/partials/alerts", params={"group": "CRM", "q": INST}).text
    assert "cpu high" in html and "disk full" not in html

    csv = client.get("/api/v1/capacity.csv", params={"group": "CRM", "instance": INST})
    assert csv.status_code == 200
    assert "crm-app-01" in csv.text and "bill-db-01" not in csv.text
    csv = client.get("/api/v1/alerts.csv", params={"group": "Billing", "q": INST})
    assert csv.status_code == 200
    assert "disk full" in csv.text and "cpu high" not in csv.text
