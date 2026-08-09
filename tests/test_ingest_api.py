"""Endpoint tests: auth, idempotency, heartbeat, and size caps."""

from __future__ import annotations

from tests.conftest import INGEST_TOKEN
from tests.test_sitescope import CONTRADICTION_FIELDS, REAL_FIELDS, _line

AUTH = {"Authorization": f"Bearer {INGEST_TOKEN}"}
URL = "/api/v1/ingest/sitescope"

BATCH = {
    "source_instance": "SIS-01",
    "heartbeat": False,
    "lines": [_line(REAL_FIELDS), _line(CONTRADICTION_FIELDS)],
}


def _active_sitescope_count(client) -> int:
    r = client.get("/api/v1/alerts", params={"active": "false"})
    return sum(1 for a in r.json() if a["source_platform"] == "sitescope")


def test_requires_bearer_token(client):
    assert client.post(URL, json=BATCH).status_code == 401
    assert client.post(URL, json=BATCH, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_ingest_and_idempotency(client):
    # Dedicated instance so the insert/update counts are independent of any
    # other test's ingest (the DB fixture is shared and test order is random).
    batch = {**BATCH, "source_instance": "SIS-IDEM"}
    # First send inserts.
    r1 = client.post(URL, json=batch, headers=AUTH).json()
    assert r1["status"] == "ok"
    assert r1["received"] == 2
    assert r1["inserted"] == 2 and r1["updated"] == 0
    after_first = _active_sitescope_count(client)

    # Same batch again -> updates in place, no new rows.
    r2 = client.post(URL, json=batch, headers=AUTH).json()
    assert r2["inserted"] == 0 and r2["updated"] == 2
    assert _active_sitescope_count(client) == after_first


def test_state_wins_makes_events_resolved(client):
    client.post(URL, json=BATCH, headers=AUTH)
    # Both sample lines are "back to default" -> OK -> resolved, so they should
    # NOT appear among ACTIVE alerts.
    active = client.get("/api/v1/alerts", params={"active": "true"}).json()
    assert not [a for a in active if a["source_platform"] == "sitescope"]


def test_no_credentials_persisted(client):
    client.post(URL, json=BATCH, headers=AUTH)
    allrows = client.get("/api/v1/alerts", params={"active": "false"}).json()
    blob = str(allrows)
    for secret in ("svcmon", "P%40ssw0rd123", "topSecretPass", "tok_abc123", "ltypeSecretVal"):
        assert secret not in blob


def test_ingest_creates_hosts(client):
    client.post(URL, json=BATCH, headers=AUTH)
    hosts = client.get("/api/v1/hosts", params={"platform": "sitescope"}).json()
    names = {h["hostname"] for h in hosts}
    assert "RemdMB-APP6" in names
    assert all(h["source_platform"] == "sitescope" for h in hosts)


def test_heartbeat_records_health_without_events(client):
    r = client.post(
        URL,
        json={"source_instance": "SIS-HB", "heartbeat": True, "lines": []},
        headers=AUTH,
    ).json()
    assert r["received"] == 0 and r["inserted"] == 0
    statuses = client.get("/api/v1/collectors/status").json()
    assert any(c["instance"] == "SIS-HB" and c["platform"] == "sitescope" for c in statuses)


def test_payload_cap(client):
    big = {"source_instance": "SIS-01", "lines": [_line(REAL_FIELDS)] * 501}
    assert client.post(URL, json=big, headers=AUTH).status_code == 413


def test_demo_ingest_lines_shares_the_push_pipeline(client):
    """The local-demo path (ingest_lines) creates the same hosts + alerts as the
    push endpoint — SiteScope shows up as a full platform with no HTTP/forwarder.
    """
    from app.db import SessionLocal
    from app.sitescope_ingest import ingest_lines

    db = SessionLocal()
    try:
        counts = ingest_lines(db, "SiteScope-DEMO", [_line(REAL_FIELDS)])
        db.commit()
    finally:
        db.close()

    assert counts.events == 1
    assert counts.hosts >= 1
    hosts = client.get("/api/v1/hosts", params={"platform": "sitescope"}).json()
    assert "RemdMB-APP6" in {h["hostname"] for h in hosts}
