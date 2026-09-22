from __future__ import annotations

from datetime import datetime, timezone

from app.collectors.mock_data import mock_nnmi_hosts, mock_zabbix_hosts
from app.db import SessionLocal
from app.models import Host, HostStatus, SourcePlatform
from app.normalizer import upsert_hosts
from app.runbook import run_common_hosts


def test_common_hosts_anchors_on_selected_instance(client):
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


def test_mock_data_gives_common_hosts_something_cross_platform_to_find(client):
    """Regression guard: MOCK_MODE hosts used to be namespaced per platform
    (Zabbix on 10.20.x.x, NNMi on 10.40.x.x, ...), so no two platforms ever
    shared an IP and this script always came back empty for the exact case
    it exists for - comparing instances across DIFFERENT tools. A couple of
    mock hosts now deliberately share an IP across platforms so the report
    has real cross-tool overlap to demonstrate, without collapsing every
    platform's estate into a single indistinguishable pool.
    """
    inst = "Zabbix-CH-TEST"
    nnmi_inst = "NNMi-CH-TEST"
    db = SessionLocal()
    try:
        upsert_hosts(db, SourcePlatform.zabbix, mock_zabbix_hosts(inst), inst)
        upsert_hosts(db, SourcePlatform.nnmi, mock_nnmi_hosts(nnmi_inst), nnmi_inst)
        db.commit()

        rows = run_common_hosts([], {"instances": f"{inst},{nnmi_inst}"})
        assert rows, "mock Zabbix and NNMi hosts should share at least one IP"
        assert {row[1] for row in rows} == {"zabbix", "nnmi"}
    finally:
        db.rollback()
        db.close()
