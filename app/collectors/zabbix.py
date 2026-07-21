"""Zabbix collector using the JSON-RPC 2.0 API.

Hosts come from ``host.get`` (with interfaces); alerts from ``trigger.get``
(only currently-firing triggers). Authentication uses an API token supplied via
the ``Authorization: Bearer`` header (Zabbix 6.4+ style).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.collectors.base import BaseCollector, CollectorError
from app.collectors import mock_data
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import normalize_zabbix_severity


class ZabbixCollector(BaseCollector):
    """Collects hosts and active triggers from Zabbix."""

    name = "zabbix"
    platform = SourcePlatform.zabbix

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._api_url = settings.zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    # --- JSON-RPC helper ----------------------------------------------------

    def _rpc(self, method: str, params: dict) -> object:
        """Call a Zabbix JSON-RPC method and return its ``result``."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        headers = {
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {self.settings.zabbix_token}",
        }
        with self._client(headers=headers) as client:
            resp = self._request_with_retries(
                client, "POST", self._api_url, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise CollectorError(f"Zabbix RPC error: {data['error']}")
        return data.get("result", [])

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_zabbix_hosts()

        result = self._rpc(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status", "available"],
                "selectInterfaces": ["ip", "main", "type"],
                "selectGroups": ["name"],
            },
        )
        hosts: list[dict] = []
        for h in result:  # type: ignore[union-attr]
            ip = None
            for iface in h.get("interfaces", []):
                if iface.get("main") == "1" or ip is None:
                    ip = iface.get("ip") or ip
            # available: 0 unknown, 1 available/up, 2 unavailable/down
            available = h.get("available", "0")
            status = {
                "1": HostStatus.up,
                "2": HostStatus.down,
            }.get(str(available), HostStatus.unknown)
            groups = h.get("groups", [])
            hosts.append(
                {
                    "external_id": h["hostid"],
                    "hostname": h.get("name") or h.get("host"),
                    "ip": ip,
                    "status": status,
                    "group_name": groups[0]["name"] if groups else None,
                    "last_seen": datetime.now(timezone.utc),
                    "raw_payload": h,
                }
            )
        return hosts

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_zabbix_alerts()

        result = self._rpc(
            "trigger.get",
            {
                "output": ["triggerid", "description", "priority", "lastchange"],
                "selectHosts": ["host", "name"],
                "only_true": True,
                "filter": {"value": 1},  # 1 = PROBLEM
                "sortfield": "lastchange",
                "sortorder": "DESC",
            },
        )
        alerts: list[dict] = []
        for t in result:  # type: ignore[union-attr]
            hosts = t.get("hosts", [])
            hostname = (hosts[0].get("name") or hosts[0].get("host")) if hosts else None
            started = None
            if t.get("lastchange"):
                started = datetime.fromtimestamp(
                    int(t["lastchange"]), tz=timezone.utc
                )
            alerts.append(
                {
                    "external_id": t["triggerid"],
                    "host_hostname": hostname,
                    "severity_int": normalize_zabbix_severity(t.get("priority", 0)),
                    "title": t.get("description", ""),
                    "started_at": started,
                    "raw_payload": t,
                }
            )
        return alerts
