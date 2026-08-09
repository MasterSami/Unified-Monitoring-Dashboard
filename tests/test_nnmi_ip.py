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


def test_ip_from_management_address():
    hosts = _collector([{"id": "1", "name": "core-sw", "managementAddress": "10.0.0.1"}]).collect_hosts()
    assert hosts[0]["ip"] == "10.0.0.1"


def test_ip_from_alternate_field_name():
    # Some NNMi versions expose it under a different key.
    hosts = _collector([{"id": "2", "name": "edge-rtr", "snmpAddress": "10.0.0.2"}]).collect_hosts()
    assert hosts[0]["ip"] == "10.0.0.2"


def test_no_ip_field_leaves_none():
    hosts = _collector([{"id": "3", "name": "dumb-hub"}]).collect_hosts()
    assert hosts[0]["ip"] is None
