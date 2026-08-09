"""NNMi host IP: resolve the management address across field-name variants."""

from __future__ import annotations

from app.collectors.nnmi import NnmiCollector
from app.config import Settings
from app.servers import ServerConfig


def _collector(records: list[dict]) -> NnmiCollector:
    cfg = ServerConfig(name="NNMi-DC1", platform="nnmi", url="http://nnmi/nnm")
    c = NnmiCollector(cfg, Settings(mock_mode=False))
    c._fetch_with_fallback = lambda *a, **k: records  # type: ignore[assignment]
    return c


def test_ip_from_active_addr():
    # activeAddr is the real field in current NNMi (confirmed from live data).
    hosts = _collector([
        {"id": "1", "name": "core0a", "longName": "core0a.corp", "activeAddr": "10.12.14.165"}
    ]).collect_hosts()
    assert hosts[0]["ip"] == "10.12.14.165"


def test_ip_from_alternate_field_name():
    # Older NNMi versions expose it under a different key.
    hosts = _collector([{"id": "2", "name": "edge-rtr", "managementAddress": "10.0.0.2"}]).collect_hosts()
    assert hosts[0]["ip"] == "10.0.0.2"


def test_ip_from_name_when_node_is_ip_only():
    # A node with no address field but whose name IS an IP.
    hosts = _collector([{"id": "3", "name": "10.0.0.9", "longName": "10.0.0.9"}]).collect_hosts()
    assert hosts[0]["ip"] == "10.0.0.9"


def test_no_ip_available_leaves_none():
    # NNMi-13 style: DNS name only, no address field.
    hosts = _collector([{"id": "4", "name": "B7_EXT_R1", "longName": "B7_EXT_R1.corp"}]).collect_hosts()
    assert hosts[0]["ip"] is None
