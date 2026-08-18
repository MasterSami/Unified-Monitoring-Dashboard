"""Zabbix Capacity metrics: parameterized keys + disk are picked up.

Regression for hosts showing no CPU/memory/disk in the Capacity view: real
Zabbix item keys carry parameters (``system.cpu.util[,idle]``,
``vm.memory.size[pused]``, ``vfs.fs.size[/,used]``), which an exact-match filter
misses. ``_attach_metrics`` now reuses the weekly report's classification
(quote-aware parsing, ranked cpu/mem selection, per-filesystem totals) plus
fallbacks for idle-only CPU and percentage-only disk templates.
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


def _item(itemid: str, hostid: str, key: str, lastvalue: str, name: str = "") -> dict:
    return {
        "itemid": itemid,
        "hostid": hostid,
        "key_": key,
        "name": name,
        "value_type": "0",
        "lastvalue": lastvalue,
    }


def test_parameterized_cpu_mem_disk_are_attached():
    # A host whose template uses idle-CPU, memory available, and absolute disk.
    items = [
        _item("1", "10", "system.cpu.util[,idle]", "83"),   # 17% busy
        _item("2", "10", "system.cpu.num", "8"),
        _item("3", "10", "vm.memory.size[total]", str(16 * GB)),
        _item("4", "10", "vm.memory.size[available]", str(4 * GB)),  # used=12
        _item("5", "10", "vfs.fs.size[/,total]", str(100 * GB)),
        _item("6", "10", "vfs.fs.size[/,used]", str(40 * GB)),
        _item("7", "10", "vfs.fs.size[/var,total]", str(50 * GB)),
        _item("8", "10", "vfs.fs.size[/var,used]", str(10 * GB)),
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
        _item("1", "20", "system.cpu.util", "45"),
        _item("2", "20", "vm.memory.utilization", "60"),
        _item("3", "20", "vfs.fs.size[C:,pused]", "72"),
    ]
    hosts = [{"external_id": "20", "metrics": {}}]
    _collector(items)._attach_metrics(hosts)
    h = hosts[0]
    assert h["cpu_pct"] == 45.0
    assert h["mem_pct"] == 60.0
    assert h["disk_pct"] == 72.0


def test_custom_named_items_attach_for_unknown_availability_hosts():
    """Friendly template names must work even when availability is unknown."""
    items = [
        _item("1", "40", "custom.cpu.cores", "56", "CPU cores"),
        _item("2", "40", "custom.cpu.percent", "43.67", "CPU usage in percent"),
        _item("3", "40", "custom.memory.total", str(1536 * GB), "Total memory"),
        _item("4", "40", "custom.memory.used", str(773.07 * GB), "Used memory"),
    ]
    hosts = [{"external_id": "40", "status": "unknown", "metrics": {}}]
    _collector(items)._attach_metrics(hosts)
    h = hosts[0]
    assert h["cpu_pct"] == 43.7
    assert h["metrics"]["cores"] == 56
    assert h["metrics"]["mem_total_gb"] == 1536.0
    assert h["metrics"]["mem_used_gb"] == 773.1
    assert h["mem_pct"] == round(773.07 / 1536 * 100, 1)


def test_ranked_cpu_prefers_non_idle_and_disk_prefers_absolute():
    # Both idle and direct CPU present -> the direct (report-ranked) one wins;
    # absolute disk sums win over a stray pused item.
    items = [
        _item("1", "30", "system.cpu.util[,idle]", "90"),
        _item("2", "30", "system.cpu.util", "55"),
        _item("3", "30", "vfs.fs.size[C:,total]", str(200 * GB), "C:: Total space"),
        _item("4", "30", "vfs.fs.size[C:,used]", str(150 * GB), "C:: Used space"),
        _item("5", "30", "vfs.fs.size[C:,pused]", "10"),
    ]
    hosts = [{"external_id": "30", "metrics": {}}]
    _collector(items)._attach_metrics(hosts)
    h = hosts[0]
    assert h["cpu_pct"] == 55.0
    assert h["metrics"]["disk_total_gb"] == 200.0
    assert h["disk_pct"] == 75.0


def test_host_without_items_is_left_untouched():
    hosts = [{"external_id": "99", "metrics": {}}]
    _collector([])._attach_metrics(hosts)
    assert "cpu_pct" not in hosts[0]
    assert hosts[0]["metrics"] == {}

