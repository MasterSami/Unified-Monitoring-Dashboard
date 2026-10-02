from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ai_poc.dynatrace_tool import get_dynatrace_problems


class FakeCollector:
    instance = "Dynatrace-POC"

    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def collect_alerts(self):
        if self.error:
            raise self.error
        return self.rows


def test_tool_returns_filtered_fact_evidence_without_raw_payload():
    now = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    collector = FakeCollector(
        [
            {"external_id": "p-1", "host_hostname": "MW10", "title": "CPU high", "severity_label": "High", "started_at": now - timedelta(minutes=5), "raw_payload": {"secret": "must-not-leak"}},
            {"external_id": "p-2", "host_hostname": "MW10", "title": "Old problem", "severity_label": "High", "started_at": now - timedelta(hours=2)},
            {"external_id": "p-3", "host_hostname": "OTHER", "title": "Other host", "severity_label": "High", "started_at": now},
        ]
    )
    result = get_dynatrace_problems(collector, hostname="mw10", time_window_minutes=60, now=now)
    payload = result.to_model_context()
    assert result.ok is True
    assert result.read_only is True
    assert [item.source_id for item in result.items] == ["p-1"]
    assert "raw_payload" not in str(payload)
    assert "secret" not in str(payload)
    assert payload["schema_version"].endswith("v0.1")


def test_tool_reports_source_unavailable_explicitly():
    result = get_dynatrace_problems(FakeCollector(error=RuntimeError("timeout")), hostname="MW10")
    assert result.ok is False
    assert result.error_code == "SOURCE_UNAVAILABLE"
    assert result.items == []


def test_tool_rejects_invalid_input():
    result = get_dynatrace_problems(FakeCollector(), hostname="", time_window_minutes=60)
    assert result.ok is False
    assert result.error_code == "INVALID_HOST"
