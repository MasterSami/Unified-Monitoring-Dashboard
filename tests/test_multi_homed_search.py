"""Search by any of a Dynatrace host's IPs, not just its primary one.

Reported: hosts visible in Dynatrace by a secondary-NIC address (10.22.68.11
through .20) never turned up in the dashboard's search. Host.ip only ever held
the first address Dynatrace reported for the host, so a search for any other
address the host also answers to found nothing — the host was not missing,
it was just indexed under an address nobody was searching for.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import Host, HostStatus, SourcePlatform
from app.normalizer import upsert_hosts
from app.routers.pages import _hosts_query

INST = "MULTIHOME-TEST"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _hostnames(db, q: str) -> set[str]:
    rows, *_ = _hosts_query(db, q, "all", "all", "hostname", "asc", INST, 1, "all")
    return {h.hostname for h in rows}


def test_a_secondary_ip_is_searchable_through_upsert(client):
    """End to end: the collector's dict reaches the DB and the search finds it."""
    db = SessionLocal()
    try:
        upsert_hosts(
            db,
            SourcePlatform.dynatrace,
            [
                {
                    "external_id": "multi-1",
                    "hostname": "app-multihomed-01",
                    "ip": "10.22.68.15",          # the primary — display value
                    "ip_all": "10.22.68.15, 192.168.44.7",
                    "status": HostStatus.up,
                }
            ],
            INST,
        )
        db.commit()

        # Findable by its primary address, as before.
        assert _hostnames(db, "10.22.68.15") == {"app-multihomed-01"}
        # And now also by the secondary one — this is the fix.
        assert _hostnames(db, "192.168.44.7") == {"app-multihomed-01"}
        assert _hostnames(db, "192.168.44") == {"app-multihomed-01"}   # substring
        # A single-IP host is unaffected: ip_all absent costs it nothing.
        assert _hostnames(db, "10.22.68.16") == set()
    finally:
        db.close()


def test_ip_all_is_never_overwritten_by_a_platform_that_lacks_it(client):
    """Only Dynatrace populates ip_all; another platform's poll must not blank it."""
    db = SessionLocal()
    try:
        row = Host(
            hostname="preexisting", source_platform=SourcePlatform.dynatrace,
            source_instance=INST, external_id="multi-2", status=HostStatus.up,
            ip="10.5.5.5", ip_all="10.5.5.5, 10.6.6.6", last_seen=NOW,
        )
        db.add(row)
        db.commit()

        # A re-poll that supplies no ip_all key at all (an older code path, or
        # a different collector reusing the same upsert) must leave it as is.
        upsert_hosts(
            db, SourcePlatform.dynatrace,
            [{"external_id": "multi-2", "hostname": "preexisting",
              "ip": "10.5.5.5", "status": HostStatus.up}],
            INST,
        )
        db.commit()
        db.refresh(row)
        assert row.ip_all == "10.5.5.5, 10.6.6.6"
    finally:
        db.close()
