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
from urllib.parse import urlparse

import httpx

from app.collectors import mock_data
from app.collectors.base import BaseCollector, CollectorError
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import normalize_zabbix_severity, zabbix_severity_label
from app.servers import ServerConfig


class ZabbixCollector(BaseCollector):
    """Collects hosts, triggers, and proxy health from one Zabbix instance."""

    name = "zabbix"
    platform = SourcePlatform.zabbix

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        super().__init__(config, settings)
        self._api_url = config.url.rstrip("/") + "/api_jsonrpc.php"
        self._auth: str | None = None
        self._tried_subpath = False

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
            # A 404 at the site root usually means the frontend lives under a
            # subpath (commonly /zabbix). Try that once before giving up.
            if resp.status_code == 404 and self._add_zabbix_subpath():
                return self._post(payload, auth=auth)
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

    def _add_zabbix_subpath(self) -> bool:
        """On a root 404, retarget the API under ``/zabbix`` (once).

        Returns True if the URL was changed and the call should be retried. Only
        fires when the configured URL had no path beyond host:port, so an
        explicit path in servers.yaml is respected.
        """
        if self._tried_subpath:
            return False
        parsed = urlparse(self._api_url)
        base_path = parsed.path.rsplit("/api_jsonrpc.php", 1)[0]
        if base_path not in ("", "/"):
            return False  # a subpath was already configured; don't guess
        self._tried_subpath = True
        self._api_url = f"{parsed.scheme}://{parsed.netloc}/zabbix/api_jsonrpc.php"
        self.logger.warning(
            "api_jsonrpc.php 404 at root; retrying under /zabbix (%s)",
            self._api_url,
        )
        return True

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
            # Host-level status: "1" means the host is unmonitored (disabled).
            if str(h.get("status")) == "1":
                status = HostStatus.disabled
            else:
                # availability: 0 unknown, 1 available/up, 2 unavailable/down.
                # Prefer the interface value (6.0+); fall back to host-level.
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

        self._attach_metrics(hosts)
        if self.config.check_proxies:
            self._summarize_proxies()
        return hosts

    # --- Capacity metrics ---------------------------------------------------
    #
    # Zabbix item keys almost always carry parameters (e.g. ``system.cpu.util[,idle]``
    # or ``vm.memory.size[pused]``) and vary by template, so an EXACT ``filter``
    # match misses them and the Capacity view stays empty. Instead we ``startSearch``
    # on the BASE keys below, then classify each returned item by its base key and
    # last parameter. Disk is summed across mounted filesystems. Fully best-effort:
    # any gap just renders "—" and never affects host collection.
    _SEARCH_BASES = ("system.cpu", "vm.memory", "vfs.fs")

    @staticmethod
    def _split_key(key: str) -> tuple[str, list[str]]:
        """Return ``(base_key, [params])`` for a Zabbix item key."""
        if "[" in key:
            base, rest = key.split("[", 1)
            return base, [p.strip() for p in rest.rstrip("]").split(",")]
        return key, []

    def _attach_metrics(self, hosts: list[dict]) -> None:
        """Attach CPU%, memory + disk (used/total and %) and cores from items.

        One bulk ``item.get`` (``startSearch`` on base keys) so parameterized keys
        from any template are picked up, then classified by base + last param.
        Memory and disk are reported as used/total (GB) plus a percentage so the
        Capacity view can show absolute consumption. Fully contained — any failure
        just leaves metrics unset ("—") and never affects host collection.
        """
        if not hosts:
            return
        try:
            items = self._rpc(
                "item.get",
                {
                    "output": ["hostid", "key_", "lastvalue"],
                    "search": {"key_": list(self._SEARCH_BASES)},
                    "searchByAny": True,
                    "startSearch": True,
                    "monitored": True,
                },
            )
        except CollectorError as exc:
            self.logger.warning("item.get for metrics failed: %s", exc)
            return

        def _new() -> dict:
            return {
                "cpu": None, "cores": None,
                "mem_pused": None, "mem_total": None,
                "mem_used": None, "mem_avail": None,
                "disk_pused": None, "disk_total": 0.0, "disk_used": 0.0,
            }

        acc: dict[str, dict] = {}
        for it in items:  # type: ignore[union-attr]
            hostid = str(it.get("hostid"))
            base, params = self._split_key(it.get("key_", ""))
            mode = params[-1] if params else ""
            try:
                val = float(it.get("lastvalue"))
            except (TypeError, ValueError):
                continue
            d = acc.setdefault(hostid, _new())
            if base == "system.cpu.util":
                busy = 100.0 - val if "idle" in params else val
                d["cpu"] = busy if d["cpu"] is None else max(d["cpu"], busy)
            elif base == "system.cpu.num":
                d["cores"] = int(val)
            elif base == "vm.memory.utilization":
                d["mem_pused"] = val
            elif base == "vm.memory.size":
                if mode == "pused":
                    d["mem_pused"] = val
                elif mode == "pavailable":
                    d["mem_pused"] = 100.0 - val
                elif mode == "used":
                    d["mem_used"] = val
                elif mode == "available":
                    d["mem_avail"] = val
                else:  # "total" or no parameter
                    d["mem_total"] = val
            elif base.endswith("fs.size"):  # vfs.fs.size / vfs.fs.dependent.size
                if mode == "pused":
                    d["disk_pused"] = val if d["disk_pused"] is None else max(d["disk_pused"], val)
                elif mode == "pfree":
                    v = 100.0 - val
                    d["disk_pused"] = v if d["disk_pused"] is None else max(d["disk_pused"], v)
                elif mode == "used":
                    d["disk_used"] += val
                elif mode in ("total", ""):
                    d["disk_total"] += val

        gb = 1024**3
        for h in hosts:
            d = acc.get(str(h.get("external_id")))
            if not d:
                continue
            metrics = h.setdefault("metrics", {})
            # CPU
            if d["cpu"] is not None:
                h["cpu_pct"] = round(d["cpu"], 1)
            if d["cores"]:
                metrics["cores"] = d["cores"]
                if d["cpu"] is not None:
                    metrics["cpu_used_cores"] = round(d["cores"] * d["cpu"] / 100, 1)
            # Memory — prefer absolute used/total, else derive from a percentage.
            total, used, avail = d["mem_total"], d["mem_used"], d["mem_avail"]
            if used is None and avail is not None and total:
                used = total - avail
            if total:
                metrics["mem_total_gb"] = round(total / gb, 1)
                if used is not None:
                    metrics["mem_used_gb"] = round(used / gb, 1)
                    h["mem_pct"] = round(used / total * 100, 1)
                elif d["mem_pused"] is not None:
                    h["mem_pct"] = round(d["mem_pused"], 1)
                    metrics["mem_used_gb"] = round(total / gb * d["mem_pused"] / 100, 1)
            elif d["mem_pused"] is not None:
                h["mem_pct"] = round(d["mem_pused"], 1)
            # Disk — sum across filesystems; prefer absolute, else a percentage.
            if d["disk_total"] > 0:
                metrics["disk_total_gb"] = round(d["disk_total"] / gb, 1)
                if d["disk_used"] > 0:
                    metrics["disk_used_gb"] = round(d["disk_used"] / gb, 1)
                    h["disk_pct"] = round(d["disk_used"] / d["disk_total"] * 100, 1)
                elif d["disk_pused"] is not None:
                    h["disk_pct"] = round(d["disk_pused"], 1)
                    metrics["disk_used_gb"] = round(d["disk_total"] / gb * d["disk_pused"] / 100, 1)
            elif d["disk_pused"] is not None:
                h["disk_pct"] = round(d["disk_pused"], 1)

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
                    "severity_label": zabbix_severity_label(t.get("priority", 0)),
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
