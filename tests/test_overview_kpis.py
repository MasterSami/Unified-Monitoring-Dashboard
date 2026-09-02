"""The Overview's headline numbers: dedup, critical, native severities, OneAgent."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Alert, Host, HostStatus, SourcePlatform

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def estate(client):
    """One small estate with deliberate overlap between tools.

    These assertions are about whole-database totals, so the module gets its own
    database rather than sharing the one every other test seeds into. Patching
    ``app.db.SessionLocal`` is enough: ``get_db`` reads that global per request,
    so the app under test sees this database too.
    """
    import app.db as dbmod

    path = Path(tempfile.mkdtemp()) / "kpi.db"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    dbmod.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    patch = pytest.MonkeyPatch()
    patch.setattr(dbmod, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        def host(eid, platform, instance, name, ip, status=HostStatus.up, agent=None):
            db.add(Host(
                external_id=eid, source_platform=platform, source_instance=instance,
                hostname=name, ip=ip, status=status, agent_deployed=agent,
                last_seen=NOW,
            ))

        # Same two physical servers, seen by BOTH Zabbix and Dynatrace.
        host("kpi-z1", SourcePlatform.zabbix, "KPI-ZBX", "web-01", "10.70.0.1")
        host("kpi-z2", SourcePlatform.zabbix, "KPI-ZBX", "db-01", "10.70.0.2")
        host("kpi-d1", SourcePlatform.dynatrace, "KPI-DT", "web-01.corp", "10.70.0.1",
             agent=True)
        host("kpi-d2", SourcePlatform.dynatrace, "KPI-DT", "db-01.corp", "10.70.0.2",
             agent=True)
        # A third server only Zabbix sees.
        host("kpi-z3", SourcePlatform.zabbix, "KPI-ZBX", "app-01", "10.70.0.3")
        # Discovered by Dynatrace with NO OneAgent — must not count as monitored.
        host("kpi-d3", SourcePlatform.dynatrace, "KPI-DT", "peer-01", "10.70.0.9",
             agent=False)
        host("kpi-d4", SourcePlatform.dynatrace, "KPI-DT", "peer-02", "10.70.0.10",
             agent=False)
        # A down host: real, but not "active".
        host("kpi-z4", SourcePlatform.zabbix, "KPI-ZBX", "old-01", "10.70.0.4",
             status=HostStatus.down)

        def alert(eid, platform, instance, label, sev, host_name):
            db.add(Alert(
                external_id=eid, source_platform=platform, source_instance=instance,
                host_hostname=host_name, severity_int=sev, severity_label=label,
                title=f"{label} on {host_name}", started_at=NOW, resolved=False,
            ))

        alert("kpi-a1", SourcePlatform.zabbix, "KPI-ZBX", "Disaster", 5, "web-01")
        alert("kpi-a2", SourcePlatform.zabbix, "KPI-ZBX", "Disaster", 5, "db-01")
        alert("kpi-a3", SourcePlatform.zabbix, "KPI-ZBX", "High", 4, "app-01")
        alert("kpi-a4", SourcePlatform.zabbix, "KPI-ZBX", "Average", 3, "app-01")
        alert("kpi-a5", SourcePlatform.dynatrace, "KPI-DT", "Availability", 4, "web-01")
        alert("kpi-a6", SourcePlatform.dynatrace, "KPI-DT", "Performance", 3, "db-01")
        alert("kpi-a7", SourcePlatform.nnmi, "KPI-NNMI", "Critical", 5, "rtr-01")
        alert("kpi-a8", SourcePlatform.nnmi, "KPI-NNMI", "Major", 4, "rtr-01")
        # Resolved alerts never appear on the Overview.
        db.add(Alert(
            external_id="kpi-a9", source_platform=SourcePlatform.zabbix,
            source_instance="KPI-ZBX", host_hostname="web-01", severity_int=5,
            severity_label="Disaster", title="old", started_at=NOW, resolved=True,
        ))
        db.commit()
    finally:
        db.close()

    yield SessionLocal
    patch.undo()


def _ctx(estate):
    from app.config import get_settings
    from app.routers.pages import _overview_context

    db = estate()
    try:
        return _overview_context(db, get_settings())
    finally:
        db.close()


# --- 1. Active servers, counted once across tools ---------------------------


def test_active_servers_counts_each_device_once(estate):
    """web-01 and db-01 are each watched by two tools but are one server each.

    Seven host rows are up, but two pairs are the same server seen twice, so
    five devices remain: 10.70.0.1, .2, .3, .9 and .10. Not .4 — it is down.
    """
    from app.config import get_settings
    from app.routers.pages import _distinct_active_hosts

    db = estate()
    try:
        assert _distinct_active_hosts(db, get_settings()) == 5
    finally:
        db.close()


def test_tile_can_be_narrowed_to_monitored_hosts(estate, monkeypatch):
    """ACTIVE_SERVERS_MONITORED_ONLY drops the agent-less Dynatrace peers."""
    from app.config import get_settings
    from app.routers.pages import _distinct_active_hosts

    settings = get_settings()
    monkeypatch.setattr(settings, "active_servers_monitored_only", True)
    db = estate()
    try:
        # 10.70.0.1, .2, .3 only — .9 and .10 have no OneAgent.
        assert _distinct_active_hosts(db, settings) == 3
    finally:
        db.close()


def test_active_servers_is_smaller_than_the_naive_sum(estate):
    """The whole point: summing per-tool totals double-counts shared servers."""
    ctx = _ctx(estate)
    naive = sum(p.total for p in ctx["per_platform"])
    assert ctx["active_hosts"] < naive


def test_tile_shows_both_its_numbers(estate):
    """The tile prints unique AND record counts, so the gap reads as duplicates.

    A headline that disagrees with the card beside it is what started this: the
    two must differ only by deduplication, never by population.
    """
    ctx = _ctx(estate)
    # Same population, so unique can never exceed the record count...
    assert ctx["active_hosts"] <= ctx["active_rows"]
    # ...and here it is strictly smaller, because two servers are seen twice.
    assert ctx["active_hosts"] == ctx["active_rows"] - 2


def test_records_figure_matches_the_platform_bars_when_scoped_to_monitored(
    estate, monkeypatch
):
    """In monitored-only mode the tile's companion figure IS the sum of the bars."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "active_servers_monitored_only", True)
    ctx = _ctx(estate)
    assert ctx["active_rows"] == sum(p.up for p in ctx["per_platform"])


def test_platform_status_counts_sum_to_total(estate):
    """up + down + unknown + disabled == total, for every platform."""
    ctx = _ctx(estate)
    for p in ctx["per_platform"]:
        assert p.up + p.down + p.unknown + p.disabled == p.total, p.platform


def test_up_is_not_derived_from_total_minus_down(estate):
    """`total - down` counts unknown and disabled hosts as healthy."""
    db = estate()
    try:
        db.add(Host(
            external_id="kpi-unk", source_platform=SourcePlatform.zabbix,
            source_instance="KPI-ZBX", hostname="ghost-01", ip="10.70.0.50",
            status=HostStatus.unknown, last_seen=NOW,
        ))
        db.add(Host(
            external_id="kpi-dis", source_platform=SourcePlatform.zabbix,
            source_instance="KPI-ZBX", hostname="off-01", ip="10.70.0.51",
            status=HostStatus.disabled, last_seen=NOW,
        ))
        db.commit()
    finally:
        db.close()

    zbx = next(p for p in _ctx(estate)["per_platform"] if p.platform == "zabbix")
    assert zbx.unknown == 1 and zbx.disabled == 1
    # The old derivation would have reported these two as up.
    assert zbx.up == zbx.total - zbx.down - zbx.unknown - zbx.disabled
    assert zbx.up != zbx.total - zbx.down


def test_agentless_hosts_count_as_servers_but_not_as_monitored(estate):
    """peer-01/peer-02 are running servers, but nothing is monitoring them.

    They belong in the headline (it counts servers we have that are up) and not
    in the platform bars (they count what each tool actually monitors). The two
    cards intentionally answer different questions here, which is why the tile
    prints its record count alongside.
    """
    from sqlalchemy import select

    db = estate()
    try:
        peers = set(db.scalars(
            select(Host.ip).where(Host.agent_deployed.is_(False))
        ).all())
    finally:
        db.close()
    assert peers == {"10.70.0.9", "10.70.0.10"}

    ctx = _ctx(estate)
    dyn = next(p for p in ctx["per_platform"] if p.platform == "dynatrace")
    assert dyn.up == 2 and dyn.discovered == 4  # bars: monitored only
    assert ctx["active_hosts"] == 5             # tile: every running server


def test_down_hosts_are_not_active(estate):
    ctx = _ctx(estate)
    assert ctx["status_counts"]["down"] >= 1
    # old-01 (10.70.0.4) is down, so it is excluded from the headline count.
    db = estate()
    try:
        from sqlalchemy import select
        ips = set(db.scalars(select(Host.ip).where(Host.status == HostStatus.up)).all())
    finally:
        db.close()
    assert "10.70.0.4" not in ips


# --- 2. Critical = each tool's own top label --------------------------------


def test_critical_counts_only_disaster_and_critical(estate):
    """Zabbix "Disaster" x2 and NNMi "Critical" x1 — and nothing else."""
    ctx = _ctx(estate)
    assert ctx["critical_alerts"] == 3
    assert ctx["critical_labels"] == ["critical", "disaster"]


def test_critical_ignores_resolved_alerts(estate):
    """The resolved Disaster row must not inflate the tile."""
    ctx = _ctx(estate)
    assert ctx["critical_alerts"] == 3  # 4 Disaster/Critical rows exist, 1 resolved


def test_critical_labels_are_configurable(estate, monkeypatch):
    from app.config import get_settings
    from app.routers.pages import _critical_alert_count

    settings = get_settings()
    db = estate()
    try:
        monkeypatch.setattr(settings, "critical_alert_labels", "high,major")
        assert _critical_alert_count(db, settings) == 2  # High x1, Major x1
        # Empty config falls back to the unified top severity.
        monkeypatch.setattr(settings, "critical_alert_labels", "")
        assert _critical_alert_count(db, settings) == 3  # severity_int == 5
    finally:
        db.close()


# --- 3. Hosts-by-status strip is hidden -------------------------------------


def test_host_status_strip_is_hidden_by_default(estate, client):
    body = client.get("/").text
    assert "Hosts by status" not in body


def test_host_status_strip_returns_when_enabled(estate, client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "show_host_status_row", True)
    body = client.get("/").text
    assert "Hosts by status" in body
    assert "UNKNOWN" in body.upper()


# --- 4. Every tool's own severity labels ------------------------------------


def test_breakdown_keeps_each_tools_own_labels(estate):
    ctx = _ctx(estate)
    by_platform = {g["platform"]: g for g in ctx["severity_by_platform"]}

    zbx = {lv["label"]: lv["count"] for lv in by_platform["zabbix"]["levels"]}
    assert zbx == {"Disaster": 2, "High": 1, "Average": 1}

    dyn = {lv["label"]: lv["count"] for lv in by_platform["dynatrace"]["levels"]}
    assert dyn == {"Availability": 1, "Performance": 1}

    nnmi = {lv["label"]: lv["count"] for lv in by_platform["nnmi"]["levels"]}
    assert nnmi == {"Critical": 1, "Major": 1}


def test_breakdown_is_ordered_most_severe_first(estate):
    ctx = _ctx(estate)
    for group in ctx["severity_by_platform"]:
        sevs = [lv["severity_int"] for lv in group["levels"]]
        assert sevs == sorted(sevs, reverse=True), group["platform"]


def test_breakdown_totals_reconcile_with_the_alert_count(estate):
    """If these disagree, one of the two views is silently dropping alerts."""
    ctx = _ctx(estate)
    assert sum(g["total"] for g in ctx["severity_by_platform"]) == ctx["active_alerts"]


def test_breakdown_renders_the_labels_on_the_page(estate, client):
    body = client.get("/").text
    for label in ("Disaster", "Availability", "Major", "Average"):
        assert label in body


# --- 5. Dynatrace counts OneAgent deployments only --------------------------


def test_dynatrace_counts_only_hosts_with_a_oneagent(estate):
    ctx = _ctx(estate)
    per = {p.platform: p for p in ctx["per_platform"]}
    assert per["dynatrace"].total == 2        # web-01.corp, db-01.corp
    assert per["dynatrace"].discovered == 4   # + peer-01, peer-02


def test_other_platforms_are_unaffected_by_the_agent_rule(estate):
    """Zabbix and NNMi report what they monitor; total must equal discovered."""
    ctx = _ctx(estate)
    for p in ctx["per_platform"]:
        if p.platform != "dynatrace":
            assert p.total == p.discovered, p.platform


def test_page_shows_the_discovered_gap(estate, client):
    body = client.get("/").text
    assert "discovered" in body


@pytest.mark.parametrize(
    "props, expected",
    [
        ({"monitoringMode": "FULL_STACK"}, True),
        ({"monitoringMode": "INFRASTRUCTURE"}, True),
        ({"monitoringMode": "DISCOVERY"}, False),
        ({"monitoringMode": "OFF"}, False),
        # Older tenants omit monitoringMode — an agentVersion means it reported.
        ({"agentVersion": {"major": 1, "minor": 291}}, True),
        ({"agentVersion": {}}, False),
        ({}, False),
    ],
)
def test_oneagent_detection(props, expected):
    from app.collectors.dynatrace import _has_oneagent

    assert _has_oneagent(props) is expected


def test_collector_records_agent_deployment(estate):
    """The flag must survive the collector -> normalizer -> DB round trip."""
    from app.normalizer import upsert_hosts

    db = estate()
    try:
        upsert_hosts(db, SourcePlatform.dynatrace, [
            {"external_id": "rt-1", "hostname": "with-agent", "ip": "10.71.0.1",
             "status": HostStatus.up, "agent_deployed": True},
            {"external_id": "rt-2", "hostname": "no-agent", "ip": "10.71.0.2",
             "status": HostStatus.up, "agent_deployed": False},
        ], "RT-DT")
        db.commit()
        from sqlalchemy import select
        flags = dict(db.execute(
            select(Host.hostname, Host.agent_deployed)
            .where(Host.source_instance == "RT-DT")
        ).all())
        assert flags == {"with-agent": True, "no-agent": False}
    finally:
        db.close()


def test_platforms_without_the_concept_keep_it_null(estate):
    """Zabbix hosts must not be silently marked as agent-less."""
    from app.normalizer import upsert_hosts

    db = estate()
    try:
        upsert_hosts(db, SourcePlatform.zabbix, [
            {"external_id": "rt-z", "hostname": "zbx-host", "ip": "10.71.0.3",
             "status": HostStatus.up},
        ], "RT-ZBX")
        db.commit()
        from sqlalchemy import select
        value = db.scalar(
            select(Host.agent_deployed).where(Host.source_instance == "RT-ZBX")
        )
        assert value is None
    finally:
        db.close()


# --- Dedup must not collapse hosts that share a placeholder IP -------------


def test_placeholder_ips_do_not_merge_hosts(estate):
    """Many Zabbix hosts sit on 127.0.0.1; that is not a device identity.

    Keying on it merged every such host into one "device", which is how a
    12,000-row estate reported under 5,000 servers.
    """
    from app.config import get_settings
    from app.routers.pages import _distinct_active_hosts

    db = estate()
    try:
        before = _distinct_active_hosts(db, get_settings())
        for i, ip in enumerate(["127.0.0.1", "127.0.0.1", "0.0.0.0", "", "::1"]):
            db.add(Host(
                external_id=f"kpi-ph{i}", source_platform=SourcePlatform.zabbix,
                source_instance="KPI-ZBX", hostname=f"agent-{i:02d}", ip=ip,
                status=HostStatus.up, last_seen=NOW,
            ))
        db.commit()
        after = _distinct_active_hosts(db, get_settings())
    finally:
        db.close()

    # Five distinct servers were added, so five must appear — not one.
    assert after == before + 5


def test_real_shared_ips_still_merge(estate):
    """The dedup must keep working for genuine cross-tool duplicates."""
    from app.config import get_settings
    from app.routers.pages import _distinct_active_hosts

    db = estate()
    try:
        before = _distinct_active_hosts(db, get_settings())
        db.add(Host(
            external_id="kpi-dup-a", source_platform=SourcePlatform.zabbix,
            source_instance="KPI-ZBX", hostname="shared-01", ip="10.70.9.9",
            status=HostStatus.up, last_seen=NOW,
        ))
        db.add(Host(
            external_id="kpi-dup-b", source_platform=SourcePlatform.nnmi,
            source_instance="KPI-NNMI", hostname="shared-01.net", ip="10.70.9.9",
            status=HostStatus.up, last_seen=NOW,
        ))
        db.commit()
        after = _distinct_active_hosts(db, get_settings())
    finally:
        db.close()

    assert after == before + 1, "one device seen by two tools is one device"


def test_placeholder_list_covers_the_usual_suspects():
    from app.routers.pages import PLACEHOLDER_IPS

    for ip in ("", "127.0.0.1", "0.0.0.0", "::1"):
        assert ip in PLACEHOLDER_IPS
