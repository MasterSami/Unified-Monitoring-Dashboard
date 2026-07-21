"""Zabbix collector using the JSON-RPC 2.0 API (one instance per server).

Authentication supports either a static API token or username/password login
(``user.login``). Hosts come from ``host.get`` (availability read from the
interface, per Zabbix 6.0+); alerts from ``trigger.get`` (currently-firing
triggers). For instances flagged ``check_proxies`` the proxy fleet health is
summarized into the run note. ``send_test_mail`` reads the instance's Email
media-type SMTP settings and sends a test email through that same relay via
``smtplib`` (the JSON-RPC API exposes no media-type test method).
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText

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
        """Send a test email to ``sendto`` via this instance's SMTP relay.

        Reads the Email media type's SMTP settings from Zabbix and sends through
        that relay with ``smtplib`` (the JSON-RPC API has no media-type test
        method on any version). Returns ``{"ok": bool, "message": str}`` and
        never raises.
        """
        if self.settings.mock_mode:
            return {
                "ok": True,
                "message": f"(mock) test mail simulated to {sendto} via {self.instance}",
            }

        # Read the Email media type's SMTP settings from Zabbix, then send the
        # test through that same relay via smtplib.
        try:
            media_types = self._rpc(
                "mediatype.get",
                {"output": "extend", "filter": {"type": "0"}},  # 0 = email
            )
        except CollectorError as exc:
            return {"ok": False, "message": str(exc)}
        if not media_types:  # type: ignore[truthy-bool]
            return {
                "ok": False,
                "message": f"No email media type configured on {self.instance}",
            }
        # Prefer an enabled media type (status == "0").
        mt = next(
            (m for m in media_types if str(m.get("status")) == "0"),  # type: ignore[union-attr]
            media_types[0],  # type: ignore[index]
        )
        server = mt.get("smtp_server")
        if not server:
            return {
                "ok": False,
                "message": f"Email media type '{mt.get('name')}' has no SMTP server",
            }
        port = int(mt.get("smtp_port") or 25)
        sender = mt.get("smtp_email") or "zabbix@localhost"
        security = str(mt.get("smtp_security", "0"))  # 0 none, 1 STARTTLS, 2 SSL
        auth = str(mt.get("smtp_authentication", "0"))  # 0 none, 1 user/pass
        username = mt.get("username", "")
        password = mt.get("passwd", "")  # usually not returned by the API

        msg = MIMEText(
            f"Test email from the Unified Monitoring Dashboard via {self.instance}.\n"
            f"If you received this, email alerting through this relay works.",
            "plain",
            "utf-8",
        )
        msg["Subject"] = f"[{self.instance}] Unified Monitoring test mail"
        msg["From"] = sender
        msg["To"] = sendto

        try:
            ctx = ssl._create_unverified_context()
            if security == "2":  # SSL/TLS on connect
                smtp = smtplib.SMTP_SSL(server, port, timeout=30, context=ctx)
            else:
                smtp = smtplib.SMTP(server, port, timeout=30)
                if security == "1":  # STARTTLS
                    smtp.starttls(context=ctx)
            if auth == "1" and username:
                smtp.login(username, password)
            smtp.sendmail(sender, [sendto], msg.as_string())
            smtp.quit()
            return {
                "ok": True,
                "message": f"Test mail sent to {sendto} via {server}:{port}",
            }
        except Exception as exc:  # noqa: BLE001 — reported to the UI
            self.logger.warning("test mail send failed: %s", exc)
            return {
                "ok": False,
                "message": f"SMTP send failed via {server}:{port} — {exc}",
            }
