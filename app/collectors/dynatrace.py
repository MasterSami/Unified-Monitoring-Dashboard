"""Dynatrace collector using the v2 REST APIs.

Hosts come from the Entities v2 API (``entitySelector=type("HOST")``). Alerts
come from Problems v2. The token scope is only guaranteed to include
read-entities, so a 403 on the problems endpoint is handled gracefully: the
alerts feed is marked unavailable and collection continues.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.collectors.base import BaseCollector
from app.collectors import mock_data
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import dynatrace_severity_label, normalize_dynatrace_severity
from app.servers import ServerConfig

_PROBLEMS_UNAVAILABLE = "unavailable — token lacks problems.read scope"


class DynatraceCollector(BaseCollector):
    """Collects hosts (Entities v2) and problems (Problems v2) from Dynatrace."""

    name = "dynatrace"
    platform = SourcePlatform.dynatrace

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        super().__init__(config, settings)
        self._base = config.url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Api-Token {self.config.token}",
            "Accept": "application/json",
        }

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_dynatrace_hosts(self.instance)

        url = f"{self._base}/api/v2/entities"
        params = {
            "entitySelector": 'type("HOST")',
            "fields": "properties,fromRelationships,tags",
            "pageSize": "500",
        }
        hosts: list[dict] = []
        with self._client(headers=self._headers()) as client:
            while url:
                resp = self._request_with_retries(
                    client, "GET", url, params=params
                )
                resp.raise_for_status()
                data = resp.json()
                for e in data.get("entities", []):
                    props = e.get("properties", {})
                    state = (props.get("state") or "").upper()
                    status = {
                        "RUNNING": HostStatus.up,
                        "UP": HostStatus.up,
                    }.get(state, HostStatus.unknown)
                    hosts.append(
                        {
                            "external_id": e.get("entityId"),
                            "hostname": e.get("displayName"),
                            "ip": (props.get("ipAddress") or [None])[0]
                            if isinstance(props.get("ipAddress"), list)
                            else props.get("ipAddress"),
                            "status": status,
                            "group_name": props.get("osType"),
                            "last_seen": datetime.now(timezone.utc),
                            "raw_payload": e,
                        }
                    )
                # Pagination: nextPageKey is passed alone on subsequent calls.
                next_key = data.get("nextPageKey")
                if next_key:
                    params = {"nextPageKey": next_key}
                else:
                    url = ""
        return hosts

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_dynatrace_alerts(self.instance)

        url = f"{self._base}/api/v2/problems"
        params = {"problemSelector": 'status("OPEN")', "pageSize": "500"}
        alerts: list[dict] = []
        with self._client(headers=self._headers()) as client:
            resp = self._request_with_retries(client, "GET", url, params=params)
            if resp.status_code == 403:
                # Token lacks problems.read — degrade gracefully.
                self.logger.warning(
                    "Dynatrace problems endpoint returned 403; %s",
                    _PROBLEMS_UNAVAILABLE,
                )
                self.notes = f"Dynatrace alerts {_PROBLEMS_UNAVAILABLE}"
                return []
            resp.raise_for_status()
            data = resp.json()
            for p in data.get("problems", []):
                affected = p.get("affectedEntities", [])
                hostname = affected[0].get("name") if affected else None
                started = None
                if p.get("startTime"):
                    started = datetime.fromtimestamp(
                        int(p["startTime"]) / 1000, tz=timezone.utc
                    )
                alerts.append(
                    {
                        "external_id": p.get("problemId"),
                        "host_hostname": hostname,
                        "severity_int": normalize_dynatrace_severity(
                            p.get("severityLevel")
                        ),
                        "severity_label": dynatrace_severity_label(
                            p.get("severityLevel")
                        ),
                        "title": p.get("title", ""),
                        "started_at": started,
                        "raw_payload": p,
                    }
                )
        return alerts
