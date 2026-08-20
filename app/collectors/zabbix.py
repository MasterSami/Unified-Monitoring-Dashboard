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
from app.owners import resolve_owner
from app.servers import ServerConfig


def _to_float_or_none(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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

        host_params = {
            "output": ["hostid", "host", "name", "status", "available"],
            "selectInterfaces": ["ip", "main", "type", "available"],
            # Owner resolution (for alert Escalate): host tags + inventory.
            "selectTags": ["tag", "value"],
            "selectInventory": ["contact", "alias"],
        }
        # Zabbix 6.2+ renamed ``selectGroups`` -> ``selectHostGroups`` (the old
        # name was removed in 7.0). Try the new name first and fall back, so host
        # groups resolve on every supported version instead of silently blanking.
        try:
            result = self._rpc(
                "host.get", {**host_params, "selectHostGroups": ["name"]}
            )
        except CollectorError:
            result = self._rpc("host.get", {**host_params, "selectGroups": ["name"]})
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
            # Response key is "hostgroups" (selectHostGroups) or "groups"
            # (selectGroups). Show ALL groups a host belongs to, not just the
            # first, so nothing appears group-less when it has several.
            groups = h.get("hostgroups") or h.get("groups") or []
            group_name = ", ".join(
                g["name"] for g in groups if g.get("name")
            ) or None
            # Inventory is a dict when populated, or an empty list when not.
            inv = h.get("inventory")
            owner, owner_email = resolve_owner(
                h.get("tags"), inv if isinstance(inv, dict) else None
            )
            hosts.append(
                {
                    "external_id": h["hostid"],
                    "hostname": h.get("name") or h.get("host"),
                    "ip": ip,
                    "status": status,
                    "group_name": group_name,
                    "owner": owner,
                    "owner_email": owner_email,
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
    # Uses the SAME item classification as the weekly report (app.zabbix_report):
    # quote-aware key parsing, ranked cpu/mem item selection, per-filesystem
    # total/used, and a history.get fallback when lastvalue is empty. Items are
    # fetched in host batches so huge instances can't blow up one giant item.get
    # (which previously failed silently and left CPU/disk blank in the UI while
    # the export — which already batched — showed everything). Fully best-effort.
    _METRIC_HOST_BATCH = 200

    def _attach_metrics(self, hosts: list[dict]) -> None:
        """Attach CPU%, memory + disk (used/total GB and %) and cores from items.

        Mirrors the weekly report's logic exactly, so whatever appears in the
        Excel report also appears in the Capacity view. Any failure just leaves
        metrics unset ("—") and never affects host collection.
        """
        if not hosts:
            return
        # Import defensively: a mismatched app/zabbix_report.py (e.g. a partial
        # file update where this collector is newer than the report module) must
        # NOT kill host collection — degrade to "no metrics" instead of raising
        # out of collect_hosts and marking the whole instance failed.
        try:
            from app.zabbix_report import (
                GB, _classify, _last_values, _KEY_SEARCH, _METRIC_NAME_SEARCH,
            )
        except ImportError as exc:
            self.logger.warning(
                "capacity metrics skipped — app/zabbix_report.py is out of sync "
                "with this collector (%s). Update that file too.", exc
            )
            note = "Capacity metrics unavailable (zabbix_report.py out of date)"
            self.notes = f"{self.notes} · {note}" if self.notes else note
            return

        hostids = [str(h["external_id"]) for h in hosts]
        by_host: dict[str, list[dict]] = {}
        for i in range(0, len(hostids), self._METRIC_HOST_BATCH):
            batch = hostids[i : i + self._METRIC_HOST_BATCH]
            try:
                items = self._rpc(
                    "item.get",
                    {
                        "hostids": batch,
                        "output": [
                            "itemid", "hostid", "key_", "name", "units",
                            "value_type", "lastvalue",
                        ],
                        "search": {
                            "key_": list(_KEY_SEARCH),
                            "name": list(_METRIC_NAME_SEARCH),
                        },
                        "searchByAny": True,
                        "startSearch": True,
                        "filter": {"status": 0},
                        "webitems": False,
                    },
                )
            except CollectorError as exc:
                self.logger.warning("item.get for metrics failed: %s", exc)
                continue
            for it in items:  # type: ignore[union-attr]
                by_host.setdefault(str(it.get("hostid")), []).append(it)

        if not by_host:
            return
        all_items = [it for lst in by_host.values() for it in lst]
        try:
            last = _last_values(self, all_items)
        except Exception as exc:  # noqa: BLE001 — lastvalue-only is still useful
            self.logger.warning("history fallback for metrics failed: %s", exc)
            last = {
                it["itemid"]: v
                for it in all_items
                if (v := _to_float_or_none(it.get("lastvalue"))) is not None
            }

        from app.zabbix_report import key_params

        for h in hosts:
            items = by_host.get(str(h.get("external_id")))
            if not items:
                continue
            c = _classify(items)

            def lv(it):
                return last.get(it.get("itemid")) if it else None

            def first_val(pred):
                """Last value of the first item whose lowercase key matches."""
                for it in items:
                    if pred(it.get("key_", "").lower()):
                        v = last.get(it.get("itemid"))
                        if v is not None:
                            return v
                return None

            metrics = h.setdefault("metrics", {})
            cores = lv(c["cpu_num"])
            cpu_now = lv(c["cpu_util"])
            if cpu_now is None:
                # Idle-only template: busy = 100 − idle.
                idle = first_val(
                    lambda k: k.startswith("system.cpu.util[") and ",idle" in k
                )
                if idle is not None:
                    cpu_now = 100.0 - idle
            if cpu_now is not None:
                h["cpu_pct"] = round(cpu_now, 1)
            if cores:
                metrics["cores"] = int(cores)
                if cpu_now is not None:
                    metrics["cpu_used_cores"] = round(int(cores) * cpu_now / 100, 1)

            mem_tot = lv(c["mem_total"])
            mem_now = lv(c["mem_util"])
            if mem_now is not None and c["mem_util_inverted"]:
                mem_now = 100.0 - mem_now
            mem_used = lv(c["mem_used"])
            if mem_tot and mem_now is None:
                # Absolute-only template: used, or total − available.
                if mem_used is None:
                    mem_used = first_val(lambda k: k == "vm.memory.size[used]")
                if mem_used is None:
                    avail = first_val(lambda k: k == "vm.memory.size[available]")
                    if avail is not None:
                        mem_used = mem_tot - avail
                if mem_used is not None:
                    mem_now = mem_used / mem_tot * 100
            if mem_tot:
                metrics["mem_total_gb"] = round(mem_tot / GB, 1)
                if mem_now is not None:
                    h["mem_pct"] = round(mem_now, 1)
                    metrics["mem_used_gb"] = round(
                        (mem_used if mem_used is not None
                         else mem_tot * mem_now / 100) / GB, 1
                    )
            elif mem_now is not None:
                h["mem_pct"] = round(mem_now, 1)

            tot_sum, used_sum = 0.0, 0.0
            for _fsname, slot in c["fs"].items():
                t = lv(slot["total"])
                u = lv(slot["used"])
                if t:
                    tot_sum += t
                if u:
                    used_sum += u
            if tot_sum > 0:
                metrics["disk_total_gb"] = round(tot_sum / GB, 1)
                metrics["disk_used_gb"] = round(used_sum / GB, 1)
                h["disk_pct"] = round(used_sum / tot_sum * 100, 1)
            else:
                # Percentage-only template: worst filesystem's pused / 100−pfree.
                worst = None
                for it in items:
                    k = it.get("key_", "").lower()
                    if not (k.startswith("vfs.fs.size[")
                            or k.startswith("vfs.fs.dependent.size[")):
                        continue
                    p = key_params(it.get("key_", ""))
                    mode = p[1].lower() if len(p) > 1 else ""
                    v = last.get(it.get("itemid"))
                    if v is None:
                        continue
                    pct = v if mode == "pused" else (100.0 - v if mode == "pfree" else None)
                    if pct is not None:
                        worst = pct if worst is None else max(worst, pct)
                if worst is not None:
                    h["disk_pct"] = round(worst, 1)

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_zabbix_alerts(self.instance)

        result = self._rpc(
            "trigger.get",
            {
                "output": ["triggerid", "description", "priority", "lastchange"],
                "selectHosts": ["hostid", "host", "name"],
                "only_true": True,
                "filter": {"value": 1},  # 1 = PROBLEM
                "sortfield": "lastchange",
                "sortorder": "DESC",
            },
        )
        alerts: list[dict] = []
        for t in result:  # type: ignore[union-attr]
            hosts = t.get("hosts", [])
            host0 = hosts[0] if hosts else {}
            hostname = host0.get("name") or host0.get("host")
            started = None
            if t.get("lastchange"):
                started = datetime.fromtimestamp(
                    int(t["lastchange"]), tz=timezone.utc
                )
            alerts.append(
                {
                    "external_id": t["triggerid"],
                    "host_hostname": hostname,
                    "host_external_id": host0.get("hostid"),
                    "severity_int": normalize_zabbix_severity(t.get("priority", 0)),
                    "severity_label": zabbix_severity_label(t.get("priority", 0)),
                    "title": t.get("description", ""),
                    "started_at": started,
                    "raw_payload": t,
                }
            )
        return alerts

    def collect_resolved_alerts(self) -> list[dict]:
        """Resolved problems from event history (last ALERT_HISTORY_DAYS days).

        ``event.get`` problem events (source 0 / object 0, value 1) that carry a
        recovery event (``r_eventid`` != 0) — i.e. problems that started AND
        resolved inside the window, regardless of when the dashboard was
        deployed. ``match_external_id`` carries the trigger id so episodes
        already recorded by live reconciliation aren't duplicated.
        """
        if self.settings.mock_mode:
            return []
        days = max(1, int(self.settings.alert_history_days))
        time_from = int(datetime.now(timezone.utc).timestamp()) - days * 86400

        # Busy instances can have far more than one page of events in the
        # window, and a single limited call sorted newest-first silently drops
        # the older ones. Page backwards through the window (newest -> oldest)
        # by moving time_till to the oldest clock seen, until exhausted.
        page_size = 5000
        max_events = 60000  # hard bound per run; the window re-scans next run
        alerts: list[dict] = []
        seen_ids: set[str] = set()
        time_till: int | None = None
        scanned = 0
        while scanned < max_events:
            params: dict = {
                "output": ["eventid", "objectid", "name", "severity",
                           "clock", "r_eventid"],
                "source": 0,          # trigger events
                "object": 0,
                "value": 1,           # PROBLEM events
                "time_from": time_from,
                "selectHosts": ["host", "name"],
                "sortfield": "clock",
                "sortorder": "DESC",
                "limit": page_size,
            }
            if time_till is not None:
                params["time_till"] = time_till
            result = self._rpc("event.get", params)
            new_in_page = 0
            oldest: int | None = None
            for ev in result:  # type: ignore[union-attr]
                eid = str(ev.get("eventid"))
                clock = int(ev.get("clock") or 0)
                oldest = clock if oldest is None else min(oldest, clock)
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                new_in_page += 1
                if str(ev.get("r_eventid") or "0") == "0":
                    continue  # still active — the live path owns it
                hosts = ev.get("hosts", [])
                hostname = (
                    (hosts[0].get("name") or hosts[0].get("host")) if hosts else None
                )
                started = (
                    datetime.fromtimestamp(clock, tz=timezone.utc) if clock else None
                )
                sev = ev.get("severity", 0)
                alerts.append(
                    {
                        "external_id": f"ev-{eid}",
                        "match_external_id": ev.get("objectid"),
                        "host_hostname": hostname,
                        "severity_int": normalize_zabbix_severity(sev),
                        "severity_label": zabbix_severity_label(sev),
                        "title": ev.get("name", ""),
                        "started_at": started,
                        "raw_payload": ev,
                    }
                )
            scanned += new_in_page
            if len(result) < page_size or new_in_page == 0 or oldest is None:  # type: ignore[arg-type]
                break
            # Next page: everything at or before the oldest clock seen (ties are
            # deduplicated via seen_ids above).
            time_till = oldest
        if scanned >= max_events:
            self.logger.warning(
                "resolved-alert backfill hit the %d-event cap for the %dd window; "
                "older events will be picked up on subsequent runs",
                max_events, days,
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

