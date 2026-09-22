from __future__ import annotations

from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import Host, HostStatus, SourcePlatform
from app.runbook import run_common_hosts


def test_common_hosts_anchors_on_selected_instance():
    db = SessionLocal()
    try:
        db.add_all([
            Host(
                source_platform=SourcePlatform.zabbix,
                source_instance="ZBX-DC1",
                external_id="zbx-common-1",
                hostname="web-01",
                ip="10.0.0.10",
                status=HostStatus.up,
                last_seen=datetime.now(timezone.utc),
                group_name="Web",
                owner="Web Team",
                agent_deployed=True,
            ),
            Host(
                source_platform=SourcePlatform.dynatrace,
                source_instance="DT-PROD",
                external_id="dt-common-1",
                hostname="web-01",
                ip="10.0.0.10",
                status=HostStatus.up,
                last_seen=datetime.now(timezone.utc),
                group_name="production",
                agent_deployed=True,
            ),
            Host(
                source_platform=SourcePlatform.nnmi,
                source_instance="NNMI-CORE",
                external_id="nnmi-only-1",
                hostname="router-01",
                ip="10.0.0.20",
                status=HostStatus.down,
            ),
        ])
        db.commit()
        rows = run_common_hosts([], {"instances": "ZBX-DC1,DT-PROD"})
        assert len(rows) == 2
        assert {row[1] for row in rows} == {"zabbix", "dynatrace"}
        assert all(row[0] == "10.0.0.10" for row in rows)
        assert any(row[5] == "Up" and row[8] == "Web Team" for row in rows)
    finally:
        db.rollback()
        db.close()
