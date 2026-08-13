"""Date-range filter (SQL) + branded xlsx export."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import openpyxl

from app.db import SessionLocal
from app.export_xlsx import build_workbook
from app.models import Alert, SourcePlatform
from app.routers.pages import _active_alerts, parse_dt

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_parse_dt_variants():
    assert parse_dt("2026-08-12T09:30") == datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)
    assert parse_dt("2026-08-12") == datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_dt("") is None and parse_dt(None) is None and parse_dt("junk") is None


def test_build_workbook_structure_and_severity_color():
    data = build_workbook(
        sheet_title="Alerts",
        period="a → b",
        filters_summary="state=active",
        columns=["Severity", "Title"],
        rows=[["Disaster", "db down", 5], ["Warning", "cert", 2]],
        severity_col=0,
    )
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    assert ws["A1"].value == "SAMI'X — Monitoring Data Export"
    assert ws["A2"].value == "Extracted from SAMI'X tool"
    assert ws["A3"].value.startswith("Period:")
    assert ws.cell(6, 1).value == "Severity"       # header row
    assert ws.freeze_panes == "A7"
    assert ws.cell(7, 1).value == "Disaster"        # severity int stripped, not shown
    assert ws.cell(7, 2).value == "db down"
    assert ws.cell(7, 1).fill.fgColor.rgb.endswith("F6D0D0")  # critical tint


def test_alerts_date_range_filters_in_sql(client):
    inst = "SIS-DATE-TEST"
    db = SessionLocal()
    try:
        db.add(Alert(external_id="d-new", source_platform=SourcePlatform.zabbix,
                     source_instance=inst, severity_int=5, severity_label="Disaster",
                     title="recent", started_at=NOW, resolved=False))
        db.add(Alert(external_id="d-old", source_platform=SourcePlatform.zabbix,
                     source_instance=inst, severity_int=3, severity_label="Average",
                     title="stale", started_at=NOW - timedelta(hours=48), resolved=False))
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        # last 24h window -> only the recent one for this instance
        rows, *_ = _active_alerts(
            db, inst, 1, NOW - timedelta(hours=24), NOW + timedelta(hours=1)
        )
        titles = {a.title for a in rows}
        assert "recent" in titles and "stale" not in titles
    finally:
        db.close()


def test_xlsx_endpoints_stream_valid_workbooks(client):
    for url, sheet in [("/api/v1/capacity.xlsx", "Capacity"),
                       ("/api/v1/alerts.xlsx?active=true", "Alerts")]:
        r = client.get(url)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/vnd.openxml")
        assert 'filename="SAMIX_' in r.headers["content-disposition"]
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        assert ws["A1"].value == "SAMI'X — Monitoring Data Export"
        assert ws.title == sheet
