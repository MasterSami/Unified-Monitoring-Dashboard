"""Forecast storage, batch run, pages and export — against a real database.

Complements ``test_capacity_forecast.py`` (which tests the maths in isolation)
by checking the parts that only break once SQL is involved: that sampling
appends instead of overwriting, that the batch fit does not issue a query per
series, and that the page and the export read the stored result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select

from app.capacity_history import record_samples
from app.db import SessionLocal, engine
from app.forecast import risk_counts, run_forecast
from app.models import (
    CapacityForecast,
    CapacityHistory,
    Host,
    HostStatus,
    SourcePlatform,
)

INST = "FORECAST-TEST"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _host(db, name: str, external_id: str, status=HostStatus.up) -> Host:
    row = Host(
        hostname=name, ip="10.9.9.1", source_platform=SourcePlatform.zabbix,
        source_instance=INST, external_id=external_id, status=status,
        group_name="Billing", last_seen=NOW,
    )
    db.add(row)
    db.commit()
    return row


def _fill(db, host: Host, subject: str, start: float, slope: float, days: int = 30):
    """Append a synthetic daily series straight to the history table."""
    db.bulk_insert_mappings(
        CapacityHistory,
        [
            {
                "host_id": host.id,
                "platform": "zabbix",
                "metric_kind": "disk",
                "subject": subject,
                "used_value": 500.0 * (start + d * slope) / 100,
                "total_value": 500.0,
                "used_pct": start + d * slope,
                "sampled_at": NOW - timedelta(days=days - 1 - d),
            }
            for d in range(days)
        ],
    )
    db.commit()


def test_sampling_appends_rather_than_overwriting(client):
    """The whole point of the table: yesterday's reading is still there."""
    db = SessionLocal()
    try:
        host = _host(db, "append-01", "append-1")
        item = {
            "external_id": "append-1",
            "cpu_pct": 30.0,
            "metrics": {
                "cores": 4, "cpu_used_cores": 1.2,
                "mem_total_gb": 32, "mem_used_gb": 16,
                "filesystems": [
                    {"subject": "/var", "used_gb": 90.0, "total_gb": 100.0}
                ],
            },
        }
        record_samples(db, "zabbix", [item], {"append-1": host}, now=NOW)
        item["metrics"]["filesystems"][0]["used_gb"] = 95.0
        record_samples(
            db, "zabbix", [item], {"append-1": host}, now=NOW + timedelta(days=1)
        )
        db.commit()

        rows = db.scalars(
            select(CapacityHistory)
            .where(CapacityHistory.host_id == host.id,
                   CapacityHistory.metric_kind == "disk")
            .order_by(CapacityHistory.sampled_at)
        ).all()
        assert [r.used_pct for r in rows] == [90.0, 95.0]
        assert {r.subject for r in rows} == {"/var"}
        # Host-level series are stored under an empty subject, never NULL —
        # a NULL there would break the uniqueness that keeps days distinct.
        mem = db.scalars(
            select(CapacityHistory).where(
                CapacityHistory.host_id == host.id,
                CapacityHistory.metric_kind == "memory",
            )
        ).first()
        assert mem is not None and mem.subject == ""
    finally:
        db.close()


def test_sampling_is_throttled_within_the_minimum_gap(client):
    """A five-minute poll must not write twelve rows an hour per series."""
    db = SessionLocal()
    try:
        host = _host(db, "throttle-01", "throttle-1")
        item = {"external_id": "throttle-1", "cpu_pct": 10.0, "metrics": {}}
        record_samples(db, "zabbix", [item], {"throttle-1": host}, now=NOW)
        record_samples(
            db, "zabbix", [item], {"throttle-1": host}, now=NOW + timedelta(minutes=5)
        )
        db.commit()
        assert db.scalar(
            select(func.count(CapacityHistory.id)).where(
                CapacityHistory.host_id == host.id
            )
        ) == 1
    finally:
        db.close()


def test_run_forecast_classifies_and_replaces(client):
    """The batch run stores one row per series and clears the previous set."""
    db = SessionLocal()
    try:
        hot = _host(db, "fc-hot-01", "fc-hot")
        cold = _host(db, "fc-cold-01", "fc-cold")
        _fill(db, hot, "/var", 59.0, 0.8)      # -> critical
        _fill(db, cold, "/data", 62.0, 0.0)    # -> ok

        run_forecast(db, now=NOW)
        rows = {
            (r.host_id, r.subject): r
            for r in db.scalars(select(CapacityForecast)).all()
        }
        assert rows[(hot.id, "/var")].classification == "critical"
        assert rows[(hot.id, "/var")].days_to_threshold_90 is not None
        assert rows[(cold.id, "/data")].classification == "ok"
        assert rows[(cold.id, "/data")].days_to_threshold_90 is None

        # Sparkline points come from the fit, not from the page.
        assert len(rows[(hot.id, "/var")].points) == 30

        before = db.scalar(select(func.count(CapacityForecast.id)))
        run_forecast(db, now=NOW)
        assert db.scalar(select(func.count(CapacityForecast.id))) == before
    finally:
        db.close()


def test_unknown_hosts_are_skipped_with_a_reason(client):
    db = SessionLocal()
    try:
        ghost = _host(db, "fc-ghost-01", "fc-ghost", status=HostStatus.unknown)
        _fill(db, ghost, "/var", 59.0, 0.8)
        run_forecast(db, now=NOW)
        row = db.scalars(
            select(CapacityForecast).where(CapacityForecast.host_id == ghost.id)
        ).first()
        assert row.classification == "insufficient_data"
        assert "unknown" in row.reason
    finally:
        db.close()


def test_cpu_series_are_stored_but_never_forecast(client):
    """A CPU percentage oscillates; extrapolating it linearly means nothing."""
    db = SessionLocal()
    try:
        host = _host(db, "fc-cpu-01", "fc-cpu")
        db.bulk_insert_mappings(
            CapacityHistory,
            [
                {
                    "host_id": host.id, "platform": "zabbix", "metric_kind": "cpu",
                    "subject": "", "used_value": None, "total_value": None,
                    "used_pct": 40.0 + d, "sampled_at": NOW - timedelta(days=29 - d),
                }
                for d in range(30)
            ],
        )
        db.commit()
        run_forecast(db, now=NOW)
        assert db.scalars(
            select(CapacityForecast).where(
                CapacityForecast.host_id == host.id,
                CapacityForecast.metric_kind == "cpu",
            )
        ).first() is None
    finally:
        db.close()


def test_forecast_run_does_not_issue_a_query_per_series(client):
    """Guards the N+1 the batching exists to avoid."""
    db = SessionLocal()
    try:
        hosts = [_host(db, f"fc-perf-{i:02d}", f"fc-perf-{i}") for i in range(12)]
        for host in hosts:
            for subject in ("/var", "/opt", "/data"):
                _fill(db, host, subject, 50.0, 0.4)

        statements: list[str] = []

        def record(conn, cursor, statement, *args):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            run_forecast(db, now=NOW)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        # 36 series across 12 hosts, in a single host batch: one hosts query,
        # one history query. Comfortably under one query per series.
        assert len(selects) <= 4, selects
    finally:
        db.close()


def test_risk_counts_feed_the_overview_card(client):
    db = SessionLocal()
    try:
        counts = risk_counts(db)
        assert counts["at_risk"] == counts.get("critical", 0) + counts.get("warning", 0)
    finally:
        db.close()


def test_forecast_page_and_partial_render(client):
    html = client.get("/capacity/planning").text
    assert "Planning" in html
    assert 'name="group"' in html and "data-svcbox" in html   # service filter
    assert "/static/svcbox.js" in html
    assert "Recompute now" in html

    partial = client.get(
        "/partials/forecast", params={"classification": "all", "kind": "disk"}
    ).text
    assert "fc-table" in partial


def test_planning_lives_under_capacity_not_in_the_sidebar(client):
    """One sidebar entry, two views — the split is a sub-nav on the page."""
    capacity = client.get("/capacity").text
    planning = client.get("/capacity/planning").text

    # Both carry the sub-nav, and each marks itself current.
    for html in (capacity, planning):
        assert 'href="/capacity/planning"' in html
        assert 'class="subnav"' in html
    assert capacity.count("aria-current=page") == 1
    assert planning.count("aria-current=page") == 1

    # The sidebar has a Capacity entry and no separate Forecast one.
    assert ">\n            Forecast\n" not in capacity
    assert "Forecast" not in capacity


def test_the_old_forecast_url_still_works(client):
    """Bookmarks and the earlier build's links must not break."""
    resp = client.get("/forecast", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].startswith("/capacity/planning")

    resp = client.get(
        "/forecast", params={"classification": "critical"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "Planning" in resp.text


def test_forecast_page_filters_narrow_the_table(client):
    db = SessionLocal()
    try:
        host = _host(db, "fc-filter-01", "fc-filter")
        _fill(db, host, "/var", 59.0, 0.8)
        run_forecast(db, now=NOW)
    finally:
        db.close()

    shown = client.get(
        "/partials/forecast", params={"classification": "critical", "q": "fc-filter"}
    ).text
    assert "fc-filter-01" in shown

    # A service that does not match must exclude it.
    hidden = client.get(
        "/partials/forecast",
        params={"classification": "critical", "q": "fc-filter", "group": "no-such"},
    ).text
    assert "fc-filter-01" not in hidden

    # So must the wrong resource kind.
    hidden = client.get(
        "/partials/forecast",
        params={"classification": "critical", "q": "fc-filter", "kind": "memory"},
    ).text
    assert "fc-filter-01" not in hidden


def test_overview_card_links_to_the_critical_list(client):
    html = client.get("/").text
    assert "Capacity Risks" in html
    assert '/capacity/planning?classification=critical' in html


def test_json_and_xlsx_exports(client):
    rows = client.get(
        "/api/v1/forecast", params={"classification": "all", "q": "fc-hot"}
    )
    assert rows.status_code == 200
    payload = rows.json()
    assert payload and payload[0]["metric_kind"] == "disk"
    assert payload[0]["classification"] == "critical"
    assert payload[0]["days_to_threshold_90"] is not None

    xlsx = client.get("/api/v1/forecast.xlsx", params={"classification": "all"})
    assert xlsx.status_code == 200
    assert xlsx.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert "capacity-forecast-" in xlsx.headers["content-disposition"]
    assert xlsx.content[:2] == b"PK"   # a real workbook


def test_manual_run_endpoint(client):
    resp = client.post("/api/v1/forecast/run")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"queued", "ok"}


class _FakeZabbix:
    """A Zabbix that answers item.get / trend.get and counts the round trips."""

    name = "zabbix"
    instance = "BATCH"
    days = 90

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def _rpc(self, method: str, params: dict):
        self.calls[method] = self.calls.get(method, 0) + 1
        if method == "item.get":
            out = []
            for hid in params["hostids"]:
                out += [
                    {"itemid": f"{hid}-cpu", "hostid": hid,
                     "key_": "system.cpu.util", "name": "CPU utilization",
                     "value_type": 0},
                    {"itemid": f"{hid}-fst", "hostid": hid,
                     "key_": "vfs.fs.size[/var,total]",
                     "name": "FS [/var]: Total space", "value_type": 3},
                    {"itemid": f"{hid}-fsu", "hostid": hid,
                     "key_": "vfs.fs.size[/var,used]",
                     "name": "FS [/var]: Used space", "value_type": 3},
                ]
            return out
        if method == "trend.get":
            import time as _t

            now, rows = int(_t.time()), []
            for iid in params["itemids"]:
                for d in range(self.days):
                    value = 50.0
                    if iid.endswith("fst"):
                        value = 500 * 1024**3
                    elif iid.endswith("fsu"):
                        value = 300 * 1024**3
                    rows.append({"itemid": iid, "clock": now - d * 86400,
                                 "num": 60, "value_avg": value})
            return rows
        return []


def test_backfill_batches_hosts_instead_of_asking_one_at_a_time(client, monkeypatch):
    """Per-host calls made a full run take hours on a real estate.

    Each host used to cost an item.get and a trend.get plus their round trips,
    so fifteen thousand hosts came to roughly thirty thousand little requests.
    Batching turns that into a couple of calls per hundred hosts.
    """
    import app.scheduler as scheduler
    from app.backfill_zabbix_capacity import HOST_BATCH, backfill_zabbix

    fake = _FakeZabbix()
    n_hosts = HOST_BATCH * 2 + 7          # forces a ragged final batch

    db = SessionLocal()
    try:
        db.add_all([
            Host(hostname=f"batch-{i:04d}", source_platform=SourcePlatform.zabbix,
                 source_instance="BATCH", external_id=f"9{i:05d}",
                 status=HostStatus.up, last_seen=datetime.now(timezone.utc))
            for i in range(n_hosts)
        ])
        db.commit()

        class _Service:
            collectors = {"BATCH": fake}

        monkeypatch.setattr(scheduler, "_service", _Service())
        monkeypatch.setattr(
            "app.backfill_zabbix_capacity.BATCH_DELAY_SECONDS", 0
        )

        written = backfill_zabbix(db, 90)
        assert written > 0

        # Three item.get calls for 207 hosts, not 207 of them.
        assert fake.calls["item.get"] == 3
        assert sum(fake.calls.values()) < n_hosts, fake.calls

        # Ninety days really landed, which is the whole point of the backfill.
        oldest, newest = db.execute(
            select(func.min(CapacityHistory.sampled_at),
                   func.max(CapacityHistory.sampled_at))
            .where(CapacityHistory.platform == "zabbix",
                   CapacityHistory.host_id.in_(
                       select(Host.id).where(Host.source_instance == "BATCH")))
        ).one()
        assert (newest - oldest).days >= 85

        # Re-running adds nothing: the keys are host + metric + drive + day.
        before = dict(fake.calls)
        assert backfill_zabbix(db, 90) == 0
        assert fake.calls["item.get"] == before["item.get"] + 3
    finally:
        db.close()


def test_no_em_dashes_in_any_rendered_template():
    """Plain hyphens throughout, so the UI reads as one hand wrote it."""
    from pathlib import Path

    offenders = [
        str(p)
        for p in Path("app/templates").rglob("*.html")
        if "—" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


def test_an_empty_page_says_why_it_is_empty(client):
    """Zeros alone cannot distinguish "nothing at risk" from "nothing fitted"."""
    db = SessionLocal()
    try:
        db.query(CapacityForecast).delete()
        db.query(CapacityHistory).delete()
        db.commit()
    finally:
        db.close()

    html = client.get("/capacity/planning").text
    assert "No capacity history yet" in html
    assert "backfill_zabbix_capacity" in html   # tells them the way out


def test_the_staleness_window_is_forgiving_enough_for_a_laptop(client):
    """A dashboard switched off over a weekend must not gate out the estate.

    ``last_seen`` only advances while the app runs, so a Monday start finds
    every host two or three days old. A tight window emptied the page for
    everyone until the first collection landed, which reads as data loss.
    """
    from app.forecast import STALE_AFTER_DAYS, _host_gate

    assert STALE_AFTER_DAYS >= 3, "a weekend is three days"

    db = SessionLocal()
    try:
        host = _host(db, "stale-01", "stale-1")
        host.last_seen = NOW - timedelta(days=3)      # closed over a weekend
        ok, why = _host_gate(host, NOW)
        assert ok, why

        host.last_seen = NOW - timedelta(days=STALE_AFTER_DAYS + 1)
        ok, why = _host_gate(host, NOW)
        assert not ok and "last seen" in why
    finally:
        db.close()
