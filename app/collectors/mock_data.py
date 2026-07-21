"""Realistic fake data for ``MOCK_MODE``.

Provides ~30 hosts and ~12 alerts spread across the three platforms so the UI
can be developed and demoed without VPN access to the real monitoring systems.
Returned dicts are already in the normalized shape consumed by the normalizer,
so mock and live paths converge at the same upsert code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import HostStatus

_NOW = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


def _ago(minutes: int) -> datetime:
    return _NOW - timedelta(minutes=minutes)


# --- Hosts ------------------------------------------------------------------

_ZABBIX_HOSTS = [
    ("zbx-web-01", "10.20.1.11", HostStatus.up, "Web Servers"),
    ("zbx-web-02", "10.20.1.12", HostStatus.up, "Web Servers"),
    ("zbx-db-01", "10.20.2.21", HostStatus.up, "Databases"),
    ("zbx-db-02", "10.20.2.22", HostStatus.down, "Databases"),
    ("zbx-cache-01", "10.20.3.31", HostStatus.up, "Cache"),
    ("zbx-lb-01", "10.20.4.41", HostStatus.up, "Load Balancers"),
    ("zbx-app-01", "10.20.5.51", HostStatus.up, "App Servers"),
    ("zbx-app-02", "10.20.5.52", HostStatus.unknown, "App Servers"),
    ("zbx-mq-01", "10.20.6.61", HostStatus.up, "Messaging"),
    ("zbx-log-01", "10.20.7.71", HostStatus.up, "Logging"),
]

_DYNATRACE_HOSTS = [
    ("dt-frontend-01", "10.30.1.11", HostStatus.up, "frontend"),
    ("dt-frontend-02", "10.30.1.12", HostStatus.up, "frontend"),
    ("dt-payments-01", "10.30.2.21", HostStatus.up, "payments"),
    ("dt-payments-02", "10.30.2.22", HostStatus.down, "payments"),
    ("dt-orders-01", "10.30.3.31", HostStatus.up, "orders"),
    ("dt-orders-02", "10.30.3.32", HostStatus.up, "orders"),
    ("dt-search-01", "10.30.4.41", HostStatus.up, "search"),
    ("dt-inventory-01", "10.30.5.51", HostStatus.up, "inventory"),
    ("dt-auth-01", "10.30.6.61", HostStatus.up, "auth"),
    ("dt-gateway-01", "10.30.7.71", HostStatus.unknown, "gateway"),
]

_NNMI_HOSTS = [
    ("core-router-01", "192.168.0.1", HostStatus.up, "Core Network"),
    ("core-router-02", "192.168.0.2", HostStatus.up, "Core Network"),
    ("dist-switch-01", "192.168.1.1", HostStatus.up, "Distribution"),
    ("dist-switch-02", "192.168.1.2", HostStatus.down, "Distribution"),
    ("access-switch-01", "192.168.2.1", HostStatus.up, "Access"),
    ("access-switch-02", "192.168.2.2", HostStatus.up, "Access"),
    ("firewall-01", "192.168.3.1", HostStatus.up, "Security"),
    ("firewall-02", "192.168.3.2", HostStatus.up, "Security"),
    ("wan-router-01", "192.168.4.1", HostStatus.up, "WAN"),
    ("wan-router-02", "192.168.4.2", HostStatus.unknown, "WAN"),
]


def _hosts(prefix: str, rows: list[tuple]) -> list[dict]:
    out: list[dict] = []
    for idx, (hostname, ip, status, group) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{prefix}-{idx}",
                "hostname": hostname,
                "ip": ip,
                "status": status,
                "group_name": group,
                "last_seen": _ago(idx),
                "raw_payload": {"mock": True, "hostname": hostname, "ip": ip},
            }
        )
    return out


def mock_zabbix_hosts() -> list[dict]:
    return _hosts("zbx-host", _ZABBIX_HOSTS)


def mock_dynatrace_hosts() -> list[dict]:
    return _hosts("dt-host", _DYNATRACE_HOSTS)


def mock_nnmi_hosts() -> list[dict]:
    return _hosts("nnmi-node", _NNMI_HOSTS)


# --- Alerts -----------------------------------------------------------------

_ZABBIX_ALERTS = [
    (5, "Database replication stopped on zbx-db-02", "zbx-db-02", 8),
    (4, "High CPU load (>90%) on zbx-app-01", "zbx-app-01", 22),
    (3, "Disk space 82% used on zbx-log-01", "zbx-log-01", 47),
    (2, "SSL certificate expires in 21 days on zbx-web-01", "zbx-web-01", 130),
]

_DYNATRACE_ALERTS = [
    (5, "Service unavailable: payments-api", "dt-payments-02", 5),
    (4, "Response time degradation on orders-service", "dt-orders-01", 18),
    (4, "Monitoring unavailable for dt-gateway-01", "dt-gateway-01", 33),
    (3, "Memory saturation on dt-search-01", "dt-search-01", 60),
]

_NNMI_ALERTS = [
    (5, "Node down: dist-switch-02", "dist-switch-02", 12),
    (4, "Interface GigabitEthernet0/1 down on core-router-01", "core-router-01", 27),
    (3, "High interface utilization on wan-router-01", "wan-router-01", 55),
    (2, "SNMP agent not responding on wan-router-02", "wan-router-02", 90),
]


def _alerts(prefix: str, rows: list[tuple]) -> list[dict]:
    out: list[dict] = []
    for idx, (sev, title, host, mins_ago) in enumerate(rows, start=1):
        out.append(
            {
                "external_id": f"{prefix}-{idx}",
                "host_hostname": host,
                "severity_int": sev,
                "title": title,
                "started_at": _ago(mins_ago),
                "raw_payload": {"mock": True, "title": title},
            }
        )
    return out


def mock_zabbix_alerts() -> list[dict]:
    return _alerts("zbx-alert", _ZABBIX_ALERTS)


def mock_dynatrace_alerts() -> list[dict]:
    return _alerts("dt-alert", _DYNATRACE_ALERTS)


def mock_nnmi_alerts() -> list[dict]:
    return _alerts("nnmi-alert", _NNMI_ALERTS)
