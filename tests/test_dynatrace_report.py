"""Dynatrace host-group resolution + per-partition disk report rows."""

from __future__ import annotations

from app.collectors.dynatrace import _host_group


def test_host_group_prefers_real_group_over_os():
    # hostGroupName wins over osType (which is not passed here — caller falls back)
    assert _host_group({"hostGroupName": "PROD-DB", "osType": "LINUX"}, None) == "PROD-DB"
    # hostGroup object form
    assert _host_group({"hostGroup": {"name": "WEB-TIER"}}, None) == "WEB-TIER"
    # group-ish tag fallback
    assert _host_group({}, [{"key": "HostGroup", "value": "APP"}]) == "APP"
    # nothing group-like -> None (caller then uses osType)
    assert _host_group({"osType": "WINDOWS"}, [{"key": "env", "value": "x"}]) is None
