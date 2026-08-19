"""Owner resolution + alert Escalate mailto."""

from __future__ import annotations

from app.owners import extract_email, owner_from_tags, resolve_owner


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
