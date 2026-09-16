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


def test_host_ips_primary_and_all():
    from app.collectors.dynatrace import _host_ips

    # Multi-homed: primary is the first address, all carries every distinct one.
    ip, ip_all = _host_ips({"ipAddress": ["10.22.68.15", "192.168.1.5"]})
    assert ip == "10.22.68.15"
    assert ip_all == "10.22.68.15, 192.168.1.5"

    # Single address: ip_all still populated, matching the same value.
    assert _host_ips({"ipAddress": ["10.22.68.15"]}) == ("10.22.68.15", "10.22.68.15")

    # No address at all.
    assert _host_ips({"ipAddress": []}) == (None, None)
    assert _host_ips({}) == (None, None)

    # Older payload shape: a bare string instead of a one-element list.
    assert _host_ips({"ipAddress": "10.1.1.1"}) == ("10.1.1.1", "10.1.1.1")

    # Duplicate addresses collapse to one entry in ip_all.
    assert _host_ips({"ipAddress": ["10.1.1.1", "10.1.1.1"]}) == (
        "10.1.1.1", "10.1.1.1",
    )
