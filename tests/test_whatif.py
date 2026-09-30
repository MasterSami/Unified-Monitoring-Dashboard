"""Capacity What-If: the engine's arithmetic, its guard rails, and the page
that sits beside Analysis and Forecasting."""

from __future__ import annotations

from uuid import uuid4

from app.db import SessionLocal
from app.models import CapacityForecast, Host, HostStatus, SourcePlatform
from app.whatif.engine import simulate, target_catalog, target_key
from app.whatif.schemas import Operation, SimulationRequest


def _seed(kind="disk", subject="/data", total=100.0, current=60.0, slope=1.0, r2=0.95, cls="watch", metrics=None):
    db = SessionLocal()
    suffix = uuid4().hex[:8]
    host = Host(
        hostname=f"whatif-{suffix}", ip=f"10.9.{int(suffix[:2], 16) % 250}.{int(suffix[2:4], 16) % 250 + 1}",
        source_platform=SourcePlatform.zabbix, source_instance="ZBX-WI", external_id=f"wi-{suffix}",
        status=HostStatus.up, cpu_pct=40.0, metrics=metrics or {},
    )
    db.add(host)
    db.flush()
    fc = CapacityForecast(
        host_id=host.id, platform="zabbix", metric_kind=kind, subject=subject,
        current_pct=current, slope_pct_per_day=slope, r_squared=r2,
        days_to_threshold_90=(90 - current) / slope if slope > 0 else None,
        days_to_full=(100 - current) / slope if slope > 0 else None,
        classification=cls, total_value=total, points=[[0, current - 10], [10, current]],
    )
    db.add(fc)
    db.commit()
    return db, host, fc


# --- arithmetic ------------------------------------------------------------


def test_steps_apply_in_order(client):
    db, host, _ = _seed()
    try:
        result = simulate(db, SimulationRequest(
            target=target_key(host.id, "disk", "/data"),
            ops=[Operation(kind="resize", absolute_total=200),
                 Operation(kind="add_once", value=20),
                 Operation(kind="growth_multiplier", factor=2)],
        ))
        # 60 GB used of 100 -> resize to 200 (30%) -> +20 GB = 80/200 (40%) -> slope x2
        assert result["scenario"]["current_pct"] == 40
        assert result["scenario"]["slope_pct_per_day"] == 2
        assert result["baseline"]["current_pct"] == 60
        assert "straight-line projection" in " ".join(result["warnings"])
    finally:
        db.close()


def test_extending_a_volume_buys_days(client):
    db, host, _ = _seed(current=83.0, slope=0.8, total=500.0)
    try:
        key = target_key(host.id, "disk", "/data")
        base = simulate(db, SimulationRequest(target=key, ops=[]))
        more = simulate(db, SimulationRequest(target=key, ops=[Operation(kind="resize", value=200)]))
        assert base["deltas"]["days_bought"] is None or base["deltas"]["days_bought"] == 0
        assert more["deltas"]["days_bought"] > 0
        assert more["scenario"]["total_value"] == 700
        assert more["scenario"]["current_pct"] < base["baseline"]["current_pct"]
    finally:
        db.close()


def test_chart_gets_a_forward_projection_that_differs_from_baseline(client):
    db, host, _ = _seed(current=50.0, slope=1.0)
    try:
        r = simulate(db, SimulationRequest(
            target=target_key(host.id, "disk", "/data"),
            ops=[Operation(kind="growth_multiplier", factor=0.5)],
        ))
        b, s = r["baseline"]["forecast_points"], r["scenario"]["forecast_points"]
        assert b and s and b[0] == [0.0, 50.0] and s[0] == [0.0, 50.0]
        assert b[-1][0] == s[-1][0] == float(r["horizon_days"])
        # Compare before either line is clamped at 100%: day 30 is 80 vs 65.
        at30 = lambda pts: next(p[1] for p in pts if p[0] == 30.0)
        assert at30(b) == 80.0 and at30(s) == 65.0
        assert r["horizon_days"] >= 90
        assert all(0 <= p[1] <= 100 for p in b + s)
    finally:
        db.close()


def test_daily_growth_in_gb_is_converted_through_the_series_size(client):
    db, host, _ = _seed(total=200.0, slope=0.5)
    try:
        r = simulate(db, SimulationRequest(
            target=target_key(host.id, "disk", "/data"),
            ops=[Operation(kind="add_daily", value=2, unit="gb")],  # 2 GB/day of 200 GB = +1 %/day
        ))
        assert r["scenario"]["slope_pct_per_day"] == 1.5
    finally:
        db.close()


def test_move_workload_updates_both_targets(client):
    db, src, _ = _seed(total=100.0, current=80.0, slope=1.0)
    db2, dst, _ = _seed(total=400.0, current=20.0, slope=0.1)
    db2.close()
    try:
        skey, dkey = target_key(src.id, "disk", "/data"), target_key(dst.id, "disk", "/data")
        r = simulate(db, SimulationRequest(
            target=skey, targets=[dkey],
            ops=[Operation(kind="move_workload", destination=dkey, size_gb=40, daily_gb=1)],
        ))
        by = {p["target"]: p for p in r["per_target"]}
        assert by[skey]["scenario"]["current_pct"] == 40          # 80 - 40 of 100
        assert by[skey]["scenario"]["slope_pct_per_day"] == 0      # 1 %/day - (1 GB/day of 100 GB)
        assert by[dkey]["scenario"]["current_pct"] == 30           # 80 + 40 of 400
        assert round(by[dkey]["scenario"]["slope_pct_per_day"], 3) == 0.35
        assert r["target"]["hostname"] == src.hostname             # info is the requested target's
    finally:
        db.close()


# --- guard rails ----------------------------------------------------------


def test_noisy_baseline_is_blocked_with_the_reason(client):
    db, host, fc = _seed(cls="noisy")
    try:
        fc.reason = "R² 0.12 below 0.3; trend too scattered to date"
        db.commit()
        try:
            simulate(db, SimulationRequest(target=target_key(host.id, "disk", "/data"), ops=[]))
        except ValueError as exc:
            assert "blocked" in str(exc).lower() and "scattered" in str(exc)
        else:
            raise AssertionError("noisy baseline must be blocked")
    finally:
        db.close()


def test_size_changes_on_a_percent_only_series_are_skipped_with_a_note(client):
    db, host, _ = _seed(kind="memory", subject="", total=None, current=70.0, slope=0.2)
    try:
        r = simulate(db, SimulationRequest(
            target=target_key(host.id, "memory", ""),
            ops=[Operation(kind="add_once", value=8), Operation(kind="add_utilization", value=10)],
        ))
        assert r["scenario"]["current_pct"] == 80                  # only the %-point step applied
        assert any("percent only" in w for w in r["warnings"])
    finally:
        db.close()


def test_memory_size_falls_back_to_the_host_metrics(client):
    db, host, _ = _seed(kind="memory", subject="", total=None, current=50.0, slope=0.1,
                        metrics={"mem_total_gb": 64})
    try:
        r = simulate(db, SimulationRequest(
            target=target_key(host.id, "memory", ""), ops=[Operation(kind="add_once", value=16)],
        ))
        assert r["scenario"]["total_value"] == 64
        assert r["scenario"]["current_pct"] == 75
    finally:
        db.close()


def test_cpu_is_utilization_only(client):
    db, host, _ = _seed()
    try:
        r = simulate(db, SimulationRequest(
            target=target_key(host.id, "cpu", ""), ops=[Operation(kind="add_utilization", value=20)],
        ))
        assert r["scenario"]["current_pct"] == 60
        assert r["scenario"]["days_to_threshold"] is None
        assert any("CPU" in w for w in r["warnings"])
    finally:
        db.close()


def test_bad_target_key_is_a_clear_error(client):
    db = SessionLocal()
    try:
        for bad in ("nonsense", "1:tape:", "x:disk:/"):
            try:
                simulate(db, SimulationRequest(target=bad, ops=[]))
            except ValueError as exc:
                assert "host id" in str(exc)
            else:
                raise AssertionError(bad)
    finally:
        db.close()


# --- catalogue ------------------------------------------------------------


def test_catalog_can_be_narrowed_to_one_host_and_carries_host_id(client):
    db, host, _ = _seed()
    try:
        items = target_catalog(db, host_id=host.id)
        ids = {i["id"] for i in items}
        assert target_key(host.id, "disk", "/data") in ids
        assert target_key(host.id, "cpu", "") in ids            # live CPU reading offered too
        assert all(i["host_id"] == host.id for i in items)
        assert all("unit" in i and "total_value" in i for i in items)
    finally:
        db.close()


# --- page + API --------------------------------------------------------------


def test_whatif_is_the_third_capacity_tab(client):
    body = client.get("/capacity/whatif").text
    assert 'href="/capacity/whatif"' in body and "aria-current=page" in body
    assert 'href="/capacity"' in body and 'href="/capacity/forecasting"' in body
    assert "Pick what to simulate" in body and "Describe the change" in body
    # No separate sidebar entry any more.
    assert 'href="/whatif"' not in body


def test_old_whatif_address_redirects_with_its_query(client):
    r = client.get("/whatif?target=1:disk:/data", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/capacity/whatif?target=1:disk:/data"


def test_simulate_api_reports_engine_errors_as_422(client):
    r = client.post("/api/v1/whatif/simulate", json={"target": "999999:disk:/nope", "ops": []})
    assert r.status_code == 422
    assert "no longer" in r.json()["detail"]


def test_scenarios_round_trip_and_export(client):
    db, host, _ = _seed()
    db.close()
    key = target_key(host.id, "disk", "/data")
    r = client.post("/api/v1/whatif/scenarios", json={
        "name": "extend 200", "target": key, "ops": [{"kind": "resize", "value": 200}],
    })
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["target"] == key

    r = client.put(f"/api/v1/whatif/scenarios/{sid}", json={
        "name": "extend 300", "target": key, "ops": [{"kind": "resize", "value": 300}],
    })
    assert r.status_code == 200 and r.json()["name"] == "extend 300"
    assert any(s["id"] == sid for s in client.get("/api/v1/whatif/scenarios").json())

    x = client.get(f"/api/v1/whatif/export?scenario_id={sid}")
    assert x.status_code == 200
    assert "SAMIX_whatif" in x.headers["content-disposition"]

    assert client.delete(f"/api/v1/whatif/scenarios/{sid}").json() == {"deleted": sid}
    assert client.get(f"/api/v1/whatif/export?scenario_id={sid}").status_code == 404


def test_table_rows_deep_link_to_the_right_target(client):
    db, host, _ = _seed()
    db.close()
    fc_page = client.get("/partials/forecast?classification=all&q=" + host.hostname).text
    # Jinja's urlencode keeps "/" as-is; the JS reads it back decoded either way.
    assert f"/capacity/whatif?target={host.id}%3Adisk%3A/data" in fc_page
    cap_page = client.get("/partials/capacity?q=" + host.hostname).text
    assert f"/capacity/whatif?host={host.id}" in cap_page
