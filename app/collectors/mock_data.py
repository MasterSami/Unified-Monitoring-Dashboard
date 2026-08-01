"""Realistic fake data for ``MOCK_MODE`` (per-instance).

Each function takes the instance name so multiple instances of the same
platform produce distinct, non-colliding records (external ids and hostnames
are namespaced by instance). Returned dicts are already in the normalized shape
consumed by the normalizer, so mock and live paths converge at the same upsert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import HostStatus

_NOW = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


def _ago(minutes: int) -> datetime:
    return _NOW - timedelta(minutes=minutes)


def _slug(instance: str) -> str:
    return instance.lower().replace(" ", "-")


# --- Host templates (hostname suffix, ip tail, status, group) --------------

_ZABBIX_HOSTS = [
    ("web-01", "11", HostStatus.up, "Web Servers"),
    ("web-02", "12", HostStatus.up, "Web Servers"),
    ("db-01", "21", HostStatus.up, "Databases"),
    ("db-02", "22", HostStatus.down, "Databases"),
    ("cache-01", "31", HostStatus.up, "Cache"),
    ("app-01", "51", HostStatus.up, "App Servers"),
    ("app-02", "52", HostStatus.unknown, "App Servers"),
    ("mq-01", "61", HostStatus.up, "Messaging"),
    ("legacy-01", "71", HostStatus.disabled, "Decommissioned"),
]

_DYNATRACE_HOSTS = [
    ("frontend-01", "11", HostStatus.up, "frontend"),
    ("payments-01", "21", HostStatus.up, "payments"),
    ("payments-02", "22", HostStatus.down, "payments"),
    ("orders-01", "31", HostStatus.up, "orders"),
    ("search-01", "41", HostStatus.up, "search"),
    ("auth-01", "61", HostStatus.up, "auth"),
    ("gateway-01", "71", HostStatus.unknown, "gateway"),
]

_NNMI_HOSTS = [
    ("core-router-01", "1", HostStatus.up, "Core Network"),
    ("core-router-02", "2", HostStatus.up, "Core Network"),
    ("dist-switch-01", "11", HostStatus.up, "Distribution"),
    ("dist-switch-02", "12", HostStatus.down, "Distribution"),
    ("firewall-01", "21", HostStatus.up, "Security"),
    ("wan-router-01", "31", HostStatus.up, "WAN"),
    ("wan-router-02", "32", HostStatus.unknown, "WAN"),
    ("old-switch-09", "99", HostStatus.disabled, "Decommissioned"),
]


def _metrics(instance: str, idx: int, name: str, status: HostStatus) -> dict:
    """Deterministic, realistic capacity metrics for one mock host.

    Disabled hosts report no metrics (they're intentionally off); every other
    host gets a spread of CPU/memory/disk utilization plus sizing extras, so the
    Capacity view has hot and cold servers to look at.
    """
    if status == HostStatus.disabled:
        return {"cpu_pct": None, "mem_pct": None, "disk_pct": None, "metrics": {}}
    seed = sum(ord(c) for c in f"{instance}:{name}")
    cpu = round(8 + (seed * 7 + idx * 13) % 86, 1)
    mem = round(20 + (seed * 5 + idx * 17) % 75, 1)
    disk = round(28 + (seed * 3 + idx * 11) % 66, 1)
    cores = [4, 8, 16, 32][seed % 4]
    mem_total = [8, 16, 32, 64, 128][seed % 5]
    disk_total = [120, 250, 500, 1000, 2000][seed % 5]
    return {
        "cpu_pct": cpu,
        "mem_pct": mem,
        "disk_pct": disk,
        "metrics": {
            "cores": cores,
            "cpu_used_cores": round(cores * cpu / 100, 1),
            "mem_total_gb": mem_total,
            "mem_used_gb": round(mem_total * mem / 100, 1),
            "disk_total_gb": disk_total,
            "disk_used_gb": round(disk_total * disk / 100, 1),
        },
    }


def _hosts(instance: str, octet: int, rows: list[tuple]) -> list[dict]:
    slug = _slug(instance)
    out: list[dict] = []
    for idx, (name, tail, status, group) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{slug}-h{idx}",
                "hostname": f"{name}.{slug}",
                "ip": f"10.{octet}.{idx}.{tail}",
                "status": status,
                "group_name": group,
                "last_seen": _ago(idx),
                **_metrics(instance, idx, name, status),
                "raw_payload": {"mock": True, "instance": instance},
            }
        )
    return out


def mock_zabbix_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 20, _ZABBIX_HOSTS)


def mock_dynatrace_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 30, _DYNATRACE_HOSTS)


def mock_nnmi_hosts(instance: str) -> list[dict]:
    return _hosts(instance, 40, _NNMI_HOSTS)


# --- Alert templates (severity, native label, title, host suffix, mins ago) -
# The native label is the source tool's OWN severity wording (Zabbix has
# Disaster/High/Average; NNMi has Critical/Major/Minor; Dynatrace has
# Availability/Performance/…), shown as-is in the UI.

_ZABBIX_ALERTS = [
    (5, "Disaster", "Database replication stopped", "db-02", 8),
    (4, "High", "High CPU load (>90%)", "app-01", 22),
    (3, "Average", "Disk space 82% used", "cache-01", 47),
    (2, "Warning", "SSL certificate expires in 21 days", "web-01", 130),
]

_DYNATRACE_ALERTS = [
    (4, "Availability", "Service unavailable: payments-api", "payments-02", 5),
    (4, "Performance", "Response time degradation on orders-service", "orders-01", 18),
    (3, "Monitoring unavailable", "Monitoring unavailable", "gateway-01", 33),
    (3, "Resource", "Memory saturation", "search-01", 60),
]

_NNMI_ALERTS = [
    (5, "Critical", "Node down", "dist-switch-02", 12),
    (4, "Major", "Interface GigabitEthernet0/1 down", "core-router-01", 27),
    (3, "Minor", "High interface utilization", "wan-router-01", 55),
    (2, "Warning", "SNMP agent not responding", "wan-router-02", 90),
]


def _alerts(instance: str, rows: list[tuple]) -> list[dict]:
    slug = _slug(instance)
    out: list[dict] = []
    for idx, (sev, label, title, host, mins_ago) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{slug}-a{idx}",
                "host_hostname": f"{host}.{slug}",
                "severity_int": sev,
                "severity_label": label,
                "title": f"{title} on {host}.{slug}",
                "started_at": _ago(mins_ago),
                "raw_payload": {"mock": True, "instance": instance},
            }
        )
    return out


def mock_zabbix_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _ZABBIX_ALERTS)


def mock_dynatrace_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _DYNATRACE_ALERTS)


def mock_nnmi_alerts(instance: str) -> list[dict]:
    return _alerts(instance, _NNMI_ALERTS)
