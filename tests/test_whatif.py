from __future__ import annotations

from uuid import uuid4

from app.db import SessionLocal
from app.models import CapacityForecast, Host, HostStatus, SourcePlatform
from app.whatif.engine import simulate, target_key
from app.whatif.schemas import Operation, SimulationRequest


def _seed():
    db = SessionLocal()
    suffix = uuid4().hex[:8]
    host = Host(hostname=f"whatif-app-{suffix}", ip=f"10.0.0.{int(suffix[:2], 16) % 200 + 1}", source_platform=SourcePlatform.zabbix, source_instance="ZBX-1", external_id=f"whatif-{suffix}", status=HostStatus.up)
    db.add(host); db.flush()
    fc = CapacityForecast(host_id=host.id, platform="zabbix", metric_kind="disk", subject="/data", current_pct=60, slope_pct_per_day=1, r_squared=.95, days_to_threshold_90=30, days_to_full=40, classification="watch", total_value=100, points=[[0, 50], [10, 60]])
    db.add(fc); db.commit(); return db, host, fc


def test_whatif_resize_and_growth_are_sequential(client):
    db, host, fc = _seed()
    try:
        result = simulate(db, SimulationRequest(target=target_key(host.id, "disk", "/data"), ops=[Operation(kind="resize", absolute_total=200), Operation(kind="add_once", value=20), Operation(kind="growth_multiplier", factor=2)]))
        assert result["scenario"]["current_pct"] == 40
        assert result["scenario"]["slope_pct_per_day"] == 2
        assert "linear projection" in " ".join(result["warnings"])
    finally:
        db.close()


def test_whatif_blocks_noisy_baseline(client):
    db, host, fc = _seed()
    try:
        fc.classification = "noisy"; fc.reason = "low R2"; db.commit()
        try:
            simulate(db, SimulationRequest(target=target_key(host.id, "disk", "/data"), ops=[]))
        except ValueError as exc:
            assert "blocked" in str(exc).lower()
        else:
            raise AssertionError("noisy baseline must be blocked")
    finally:
        db.close()
