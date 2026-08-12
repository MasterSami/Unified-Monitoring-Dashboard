"""Shared-devices page: SQL ranking (share count desc) + server-side pagination."""

from __future__ import annotations

from app.db import SessionLocal
from app.models import Host, HostStatus, SourcePlatform
from app.routers.pages import PAGE_SIZE, _shared_host_groups


def _host(ip, instance, platform=SourcePlatform.zabbix, name=None):
    return Host(
        hostname=name or f"h-{ip}-{instance}",
        ip=ip,
        source_platform=platform,
        source_instance=instance,
        external_id=f"{instance}:{ip}",
        status=HostStatus.up,
    )


def test_ranked_by_share_count_desc(client):
    db = SessionLocal()
    try:
        # 10.0.0.1 shared by 3 instances; 10.0.0.2 by 2; 10.0.0.9 by 1 (excluded).
        db.add_all([
            _host("10.0.0.1", "Zabbix-66"),
            _host("10.0.0.1", "Zabbix-67"),
            _host("10.0.0.1", "NNMi-13", SourcePlatform.nnmi),
            _host("10.0.0.2", "Zabbix-66"),
            _host("10.0.0.2", "Zabbix-67"),
            _host("10.0.0.9", "Zabbix-66"),  # only one instance -> not shared
        ])
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        groups, total, page, pages = _shared_host_groups(db, 1)
    finally:
        db.close()

    ips = [g["ip"] for g in groups]
    assert "10.0.0.9" not in ips  # single-instance excluded
    # Most-shared first: 10.0.0.1 (3) before 10.0.0.2 (2).
    assert ips.index("10.0.0.1") < ips.index("10.0.0.2")
    assert next(g for g in groups if g["ip"] == "10.0.0.1")["instance_count"] == 3
    assert total >= 2 and page == 1 and pages >= 1


def test_pagination_bounds(client):
    db = SessionLocal()
    try:
        _, total, page, pages = _shared_host_groups(db, 999)
    finally:
        db.close()
    # Page is clamped to the last page; never exceeds `pages`.
    assert page == pages
    assert pages == max(1, -(-total // PAGE_SIZE))
