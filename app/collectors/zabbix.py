"""Zabbix collector using the JSON-RPC 2.0 API (one instance per server).

Authentication supports either a static API token or username/password login
(``user.login``). Hosts come from ``host.get`` (availability read from the
interface, per Zabbix 6.0+); alerts from ``trigger.get`` (currently-firing
triggers). For instances flagged ``check_proxies`` the proxy fleet health is
summarized into the run note. A ``send_test_mail`` action triggers Zabbix to
send a test email via ``mediatype.test``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.collectors import mock_data
from app.collectors.base import BaseCollector, CollectorError
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import normalize_zabbix_severity
from app.servers import ServerConfig


class ZabbixCollector(BaseCollector):
    """Collects hosts, triggers, and proxy health from one Zabbix instance."""

    name = "zabbix"
    platform = SourcePlatform.zabbix

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        super().__init__(config, settings)
        self._api_url = config.url.rstrip("/") + "/api_jsonrpc.php"
        self._auth: str | None = None

    # --- JSON-RPC plumbing --------------------------------------------------

    def _post(self, payload: dict, auth: str | None = None) -> dict:
        """POST a JSON-RPC payload and return the decoded response dict."""
        headers = {"Content-Type": "application/json-rpc"}
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        try:
            with self._client(headers=headers) as client:
                resp = self._request_with_retries(
                    client, "POST", self._api_url, json=payload
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.ConnectError as exc:
            # A plain-HTTP server reached over https:// surfaces as this SSL
            # error. Transparently retry once over http:// and stick with it.
            if (
                "WRONG_VERSION_NUMBER" in str(exc)
                and self._api_url.startswith("https://")
            ):
                self.logger.warning(
                    "TLS failed for %s; retrying over http:// (server appears "
                    "to be plain HTTP)",
                    self._api_url,
                )
                self._api_url = "http://" + self._api_url[len("https://"):]
                return self._post(payload, auth=auth)
            raise

    def _login(self) -> str:
        """Return an auth token, logging in with user/password if needed."""
        if self._auth:
            return self._auth
        if self.config.token:
            self._auth = self.config.token
            return self._auth

        # Zabbix 5.4+ uses "username"; older releases used "user".
        last_error: object = None
        for user_key in ("username", "user"):
            data = self._post(
                {
                    "jsonrpc": "2.0",
                    "method": "user.login",
                    "params": {
                        user_key: self.config.user,
                        "password": self.config.password,
                    },
                    "id": 1,
                }
            )
            if "result" in data:
                self._auth = str(data["result"])
                return self._auth
            last_error = data.get("error", data)
        raise CollectorError(f"Zabbix login failed: {last_error}")

    def _rpc(self, method: str, params: dict) -> object:
        """Call an authenticated Zabbix method and return its ``result``."""
        auth = self._login()
        data = self._post(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            auth=auth,
        )
        if "error" in data:
            raise CollectorError(f"Zabbix {method} error: {data['error']}")
        return data.get("result", [])

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            if self.config.check_proxies:
                self.notes = f"Proxies 3/3 online: {self.instance}-prx-a, {self.instance}-prx-b, {self.instance}-prx-c"
            return mock_data.mock_zabbix_hosts(self.instance)

        result = self._rpc(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status", "available"],
                "selectInterfaces": ["ip", "main", "type", "available"],
                "selectGroups": ["name"],
            },
        )
        hosts: list[dict] = []
        for h in result:  # type: ignore[union-attr]
            ip = None
            iface_available: str | None = None
            for iface in h.get("interfaces", []):
                is_main = iface.get("main") == "1"
                if is_main or ip is None:
                    ip = iface.get("ip") or ip
                if is_main or iface_available is None:
                    iface_available = iface.get("available", iface_available)
            # 0 unknown, 1 available/up, 2 unavailable/down. Prefer the
            # interface value (6.0+); fall back to the host-level field.
            available = iface_available
            if available in (None, "0"):
                available = h.get("available", available or "0")
            status = {"1": HostStatus.up, "2": HostStatus.down}.get(
                str(available), HostStatus.unknown
            )
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

        if self.config.check_proxies:
            self._summarize_proxies()
        return hosts

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_zabbix_alerts(self.instance)

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
            hostname = (
                (hosts[0].get("name") or hosts[0].get("host")) if hosts else None
            )
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

    # --- Proxy health -------------------------------------------------------

    def _summarize_proxies(self) -> None:
        """Fetch proxy fleet health and record a summary in ``self.notes``."""
        try:
            result = self._rpc("proxy.get", {"output": "extend"})
        except CollectorError as exc:
            self.logger.warning("proxy.get failed: %s", exc)
            return
        now = datetime.now(timezone.utc).timestamp()
        # A proxy that reported within this window is considered online.
        threshold = max(600, self.settings.poll_interval_minutes * 60 * 2)
        online: list[str] = []
        offline: list[str] = []
        for p in result:  # type: ignore[union-attr]
            pname = p.get("name") or p.get("host") or p.get("proxyid")
            last = p.get("lastaccess")
            try:
                last_ts = int(last)
            except (TypeError, ValueError):
                last_ts = 0
            if last_ts and (now - last_ts) <= threshold:
                online.append(str(pname))
            else:
                offline.append(str(pname))
        total = len(online) + len(offline)
        if total == 0:
            self.notes = "No proxies configured"
        elif offline:
            self.notes = (
                f"Proxies {len(online)}/{total} online — "
                f"OFFLINE: {', '.join(offline)}"
            )
        else:
            self.notes = f"Proxies {len(online)}/{total} online: {', '.join(online)}"

    # --- Test mail ----------------------------------------------------------

    def send_test_mail(self, sendto: str) -> dict:
        """Ask Zabbix to send a test email to ``sendto`` via its email media.

        Returns ``{"ok": bool, "message": str}``. Never raises.
        """
        if self.settings.mock_mode:
            return {
                "ok": True,
                "message": f"(mock) test mail simulated to {sendto} via {self.instance}",
            }

        version = self._api_version()
        try:
            media_types = self._rpc(
                "mediatype.get",
                {"output": ["mediatypeid", "name", "type"], "filter": {"type": 0}},
            )
            if not media_types:  # type: ignore[truthy-bool]
                return {
                    "ok": False,
                    "message": f"No email media type configured on {self.instance}",
                }
            mt = media_types[0]  # type: ignore[index]
            result = self._rpc(
                "mediatype.test",
                {
                    "mediatypeid": mt["mediatypeid"],
                    "sendto": sendto,
                    "subject": "Unified Monitoring — test mail",
                    "message": (
                        f"Test email from the Unified Monitoring Dashboard via "
                        f"{self.instance}. If you received this, email alerting works."
                    ),
                },
            )
            return {
                "ok": True,
                "message": f"Test mail sent to {sendto} via '{mt.get('name')}'",
                "detail": result,
            }
        except CollectorError as exc:
            msg = str(exc)
            # The JSON-RPC API doesn't expose a media-type test on any version
            # (the UI "Test" button uses an internal controller, not the API).
            if "-32601" in msg or "not found" in msg.lower():
                return {
                    "ok": False,
                    "message": (
                        f"Zabbix {version or '?'} JSON-RPC API has no test-mail "
                        f"method (the UI Test button uses an internal endpoint, "
                        f"not the API). Send a test from the Zabbix UI: "
                        f"Alerts → Media types → (email) → Test."
                    ),
                }
            return {"ok": False, "message": msg}
        except Exception as exc:  # noqa: BLE001 — reported to the UI
            self.logger.warning("test mail failed: %s", exc)
            return {"ok": False, "message": str(exc)}

    def _api_version(self) -> str:
        """Best-effort Zabbix API version (empty string if it can't be read)."""
        try:
            data = self._post(
                {"jsonrpc": "2.0", "method": "apiinfo.version", "params": {}, "id": 1}
            )
            return str(data.get("result", ""))
        except Exception:  # noqa: BLE001 — informational only
            return ""
