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


# --- Alert templates (severity, title, host suffix, minutes ago) -----------

_ZABBIX_ALERTS = [
    (5, "Database replication stopped", "db-02", 8),
    (4, "High CPU load (>90%)", "app-01", 22),
    (3, "Disk space 82% used", "cache-01", 47),
    (2, "SSL certificate expires in 21 days", "web-01", 130),
]

_DYNATRACE_ALERTS = [
    (5, "Service unavailable: payments-api", "payments-02", 5),
    (4, "Response time degradation on orders-service", "orders-01", 18),
    (4, "Monitoring unavailable", "gateway-01", 33),
    (3, "Memory saturation", "search-01", 60),
]

_NNMI_ALERTS = [
    (5, "Node down", "dist-switch-02", 12),
    (4, "Interface GigabitEthernet0/1 down", "core-router-01", 27),
    (3, "High interface utilization", "wan-router-01", 55),
    (2, "SNMP agent not responding", "wan-router-02", 90),
]


def _alerts(instance: str, rows: list[tuple]) -> list[dict]:
    slug = _slug(instance)
    out: list[dict] = []
    for idx, (sev, title, host, mins_ago) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{slug}-a{idx}",
                "host_hostname": f"{host}.{slug}",
                "severity_int": sev,
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
