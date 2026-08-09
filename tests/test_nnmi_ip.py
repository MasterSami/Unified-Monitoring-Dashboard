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


def test_ip_from_ipaddressbean_when_node_has_no_address():
    # NNMi-13 style: DNS name only on the node; IP comes from IPAddressBean,
    # joined by node id. IPAddressBean is only consulted because the node lacks
    # a direct address.
    c = _collector([{"id": "555", "name": "B7_EXT_R1", "longName": "B7_EXT_R1.corp"}])
    c._fetch_ip_by_node = lambda: {"555": "10.19.70.13"}  # type: ignore[assignment]
    hosts = c.collect_hosts()
    assert hosts[0]["ip"] == "10.19.70.13"


def test_no_ip_anywhere_leaves_none():
    c = _collector([{"id": "4", "name": "B7_EXT_R1", "longName": "B7_EXT_R1.corp"}])
    c._fetch_ip_by_node = lambda: {}  # IPAddressBean has nothing for it
    hosts = c.collect_hosts()
    assert hosts[0]["ip"] is None


def test_ipaddressbean_failure_never_breaks_host_collection():
    # A 500 (or any error) from IPAddressBean must not fail host collection —
    # hosts still come back, just without the enriched IP.
    from app.collectors.base import CollectorError

    c = _collector([{"id": "9", "name": "B7_EXT_R1", "longName": "B7_EXT_R1.corp"}])

    def boom(*a, **k):
        raise CollectorError("500 Internal Server Error")

    c._fetch = boom  # type: ignore[assignment]  # IPAddressBean call raises
    hosts = c.collect_hosts()  # must not raise
    assert len(hosts) == 1 and hosts[0]["ip"] is None


def test_ipaddressbean_join_and_loopback_filter():
    # _fetch_ip_by_node keeps the first non-loopback IPv4 per node.
    from app.collectors.nnmi import NnmiCollector
    from app.config import Settings
    from app.servers import ServerConfig
    c = NnmiCollector(ServerConfig(name="NNMi-13", platform="nnmi", url="http://n"), Settings(mock_mode=False))
    c._fetch = lambda *a, **k: [  # type: ignore[assignment]
        {"hostedOnId": "1", "ipValue": "127.0.0.1"},   # loopback -> skipped
        {"hostedOnId": "1", "ipValue": "10.0.0.5"},    # kept
        {"hostedOnId": "1", "ipValue": "10.0.0.6"},    # node already has one
        {"hostedOnId": "2", "ipValue": "192.168.1.9"},
    ]
    assert c._fetch_ip_by_node() == {"1": "10.0.0.5", "2": "192.168.1.9"}
