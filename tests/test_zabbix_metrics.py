"""Zabbix Capacity metrics: parameterized keys + disk are picked up.

Regression for hosts showing no CPU/memory/disk in the Capacity view: real
Zabbix item keys carry parameters (``system.cpu.util[,idle]``,
``vm.memory.size[pused]``, ``vfs.fs.size[/,used]``), which an exact-match filter
misses. ``_attach_metrics`` must classify by base key + parameter instead.
"""

from __future__ import annotations

from app.collectors.zabbix import ZabbixCollector
from app.config import Settings
from app.servers import ServerConfig

GB = 1024 ** 3


def _collector(items: list[dict]) -> ZabbixCollector:
    cfg = ServerConfig(name="Zabbix-34", platform="zabbix", url="http://z")
    c = ZabbixCollector(cfg, Settings(mock_mode=False))
    c._rpc = lambda method, params: items  # type: ignore[assignment]
    return c


def test_parameterized_cpu_mem_disk_are_attached():
    # A host whose template uses idle-CPU, memory available%, and absolute disk.
    items = [
        {"hostid": "10", "key_": "system.cpu.util[,idle]", "lastvalue": "83"},   # 17% busy
        {"hostid": "10", "key_": "system.cpu.num", "lastvalue": "8"},
        {"hostid": "10", "key_": "vm.memory.size[total]", "lastvalue": str(16 * GB)},
        {"hostid": "10", "key_": "vm.memory.size[available]", "lastvalue": str(4 * GB)},  # used=12
        {"hostid": "10", "key_": "vfs.fs.size[/,total]", "lastvalue": str(100 * GB)},
        {"hostid": "10", "key_": "vfs.fs.size[/,used]", "lastvalue": str(40 * GB)},
        {"hostid": "10", "key_": "vfs.fs.size[/var,total]", "lastvalue": str(50 * GB)},
        {"hostid": "10", "key_": "vfs.fs.size[/var,used]", "lastvalue": str(10 * GB)},
    ]
    hosts = [{"external_id": "10", "metrics": {}}]
    _collector(items)._attach_metrics(hosts)
    h = hosts[0]
    assert h["cpu_pct"] == 17.0
    assert h["metrics"]["cores"] == 8
    assert h["metrics"]["cpu_used_cores"] == round(8 * 17 / 100, 1)
    assert h["metrics"]["mem_total_gb"] == 16.0
    assert h["metrics"]["mem_used_gb"] == 12.0
    assert h["mem_pct"] == 75.0
    # Disk summed across / and /var: 150 total, 50 used -> 33.3%
    assert h["metrics"]["disk_total_gb"] == 150.0
    assert h["metrics"]["disk_used_gb"] == 50.0
    assert h["disk_pct"] == round(50 / 150 * 100, 1)


def test_percentage_only_keys_still_populate():
    # Windows-ish host: only percentages available (no absolute totals).
    items = [
        {"hostid": "20", "key_": "system.cpu.util", "lastvalue": "45"},
        {"hostid": "20", "key_": "vm.memory.utilization", "lastvalue": "60"},
        {"hostid": "20", "key_": "vfs.fs.size[C:,pused]", "lastvalue": "72"},
    ]
    hosts = [{"external_id": "20", "metrics": {}}]
    _collector(items)._attach_metrics(hosts)
    h = hosts[0]
    assert h["cpu_pct"] == 45.0
    assert h["mem_pct"] == 60.0
    assert h["disk_pct"] == 72.0


def test_host_without_items_is_left_untouched():
    hosts = [{"external_id": "99", "metrics": {}}]
    _collector([])._attach_metrics(hosts)
    assert "cpu_pct" not in hosts[0]
    assert hosts[0]["metrics"] == {}
