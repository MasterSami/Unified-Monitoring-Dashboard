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
from app.owners import owner_from_tags
from app.servers import ServerConfig

_PROBLEMS_UNAVAILABLE = "unavailable — token lacks problems.read scope"

_GB = 1024**3


def _host_group(props: dict, tags: list | None) -> str | None:
    """Best real host-group name for a Dynatrace HOST entity, or None.

    Prefers the assigned host group (``hostGroupName`` / ``hostGroup.name``);
    otherwise looks for a group-ish tag (key contains "group"). Never returns
    the OS type — the caller falls back to that separately.
    """
    name = props.get("hostGroupName")
    if not name:
        hg = props.get("hostGroup")
        if isinstance(hg, dict):
            name = hg.get("name") or hg.get("displayName")
    if name:
        return str(name)
    for t in tags or []:
        if not isinstance(t, dict):
            continue
        key = (t.get("key") or "").lower()
        if "group" in key and t.get("value"):
            return str(t["value"])
    return None


def _first_host_rel(entity: dict) -> str | None:
    """Owning HOST entity id for a DISK entity (from/to relationships)."""
    for bag in (entity.get("fromRelationships"), entity.get("toRelationships")):
        if not isinstance(bag, dict):
            continue
        for key, lst in bag.items():
            if "disk" not in key.lower() and "host" not in key.lower():
                continue
            for item in lst or []:
                rid = item.get("id") if isinstance(item, dict) else item
                if isinstance(rid, str) and rid.startswith("HOST-"):
                    return rid
    return None


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
                    # Capacity totals come straight from entity properties (no
                    # extra API call): physical memory (bytes) and CPU cores.
                    metrics: dict = {}
                    phys = props.get("physicalMemory")
                    if isinstance(phys, (int, float)) and phys > 0:
                        metrics["mem_total_gb"] = round(phys / 1024**3, 1)
                    cores = props.get("cpuCores") or props.get("logicalCpuCores")
                    if isinstance(cores, (int, float)) and cores > 0:
                        metrics["cores"] = int(cores)
                    owner, owner_email = owner_from_tags(e.get("tags"))
                    hosts.append(
                        {
                            "external_id": e.get("entityId"),
                            "hostname": e.get("displayName"),
                            "ip": (props.get("ipAddress") or [None])[0]
                            if isinstance(props.get("ipAddress"), list)
                            else props.get("ipAddress"),
                            "status": status,
                            # Real Dynatrace host group (not the OS). Falls back to
                            # a group-ish tag, then OS type, so the column is never
                            # blank for hosts that aren't in a host group.
                            "group_name": _host_group(props, e.get("tags"))
                            or props.get("osType"),
                            "owner": owner,
                            "owner_email": owner_email,
                            "last_seen": datetime.now(timezone.utc),
                            "metrics": metrics,
                            "raw_payload": e,
                        }
                    )
                # Pagination: nextPageKey is passed alone on subsequent calls.
                next_key = data.get("nextPageKey")
                if next_key:
                    params = {"nextPageKey": next_key}
                else:
                    url = ""
        self._attach_metrics(hosts)
        return hosts

    # --- Capacity metrics (Metrics v2) --------------------------------------

    #: Usage metrics pulled for the Capacity view. Disk is split+summed per host
    #: so multi-disk hosts roll up to one total.
    _METRIC_SELECTOR = ",".join(
        (
            "builtin:host.cpu.usage",
            "builtin:host.mem.usage",
            'builtin:host.disk.used:splitBy("dt.entity.host")',
            'builtin:host.disk.avail:splitBy("dt.entity.host")',
        )
    )

    def _attach_metrics(self, hosts: list[dict]) -> None:
        """Best-effort: attach CPU%/memory/disk usage from the Metrics v2 API.

        One paged ``metrics/query`` for CPU%, memory%, and disk used/avail
        (bytes, summed per host). Memory used is derived from the host's physical
        memory × usage%. Fully contained: on any error (including a token
        without ``metrics.read``) hosts keep whatever totals came from their
        properties and usage is simply left blank.
        """
        if not hosts:
            return
        by_id = {h["external_id"]: h for h in hosts}
        cpu: dict[str, float] = {}
        mem: dict[str, float] = {}
        disk_used: dict[str, float] = {}
        disk_avail: dict[str, float] = {}

        url = f"{self._base}/api/v2/metrics/query"
        params: dict[str, str] = {
            "metricSelector": self._METRIC_SELECTOR,
            "entitySelector": 'type("HOST")',
            "from": "now-30m",
            "resolution": "Inf",
            "pageSize": "5000",
        }
        try:
            with self._client(headers=self._headers()) as client:
                while True:
                    resp = self._request_with_retries(
                        client, "GET", url, params=params
                    )
                    if resp.status_code == 403:
                        self.logger.warning(
                            "Dynatrace metrics 403 — token lacks metrics.read"
                        )
                        note = "Capacity metrics unavailable (token lacks metrics.read)"
                        self.notes = f"{self.notes} · {note}" if self.notes else note
                        return
                    resp.raise_for_status()
                    payload = resp.json()
                    for series in payload.get("result", []):
                        mid = series.get("metricId", "")
                        for pt in series.get("data", []):
                            dims = pt.get("dimensions", [])
                            hid = dims[0] if dims else None
                            vals = [v for v in pt.get("values", []) if v is not None]
                            if not hid or not vals:
                                continue
                            val = float(vals[-1])
                            if "cpu.usage" in mid:
                                cpu[hid] = val
                            elif "mem.usage" in mid:
                                mem[hid] = val
                            elif "disk.used" in mid:
                                disk_used[hid] = disk_used.get(hid, 0.0) + val
                            elif "disk.avail" in mid:
                                disk_avail[hid] = disk_avail.get(hid, 0.0) + val
                    next_key = payload.get("nextPageKey")
                    if not next_key:
                        break
                    params = {"nextPageKey": next_key}
        except Exception as exc:  # noqa: BLE001 — contained; metrics are optional
            self.logger.warning("Dynatrace metrics query failed: %s", exc)
            return

        for hid, h in by_id.items():
            m = h.setdefault("metrics", {})
            if hid in cpu:
                h["cpu_pct"] = round(cpu[hid], 1)
                if m.get("cores"):
                    m["cpu_used_cores"] = round(m["cores"] * cpu[hid] / 100, 1)
            if hid in mem:
                h["mem_pct"] = round(mem[hid], 1)
                if m.get("mem_total_gb"):
                    m["mem_used_gb"] = round(m["mem_total_gb"] * mem[hid] / 100, 1)
            if hid in disk_used and hid in disk_avail:
                used = disk_used[hid]
                total = used + disk_avail[hid]
                if total > 0:
                    m["disk_total_gb"] = round(total / 1024**3, 1)
                    m["disk_used_gb"] = round(used / 1024**3, 1)
                    h["disk_pct"] = round(used / total * 100, 1)

    # --- Per-partition disk (for the capacity report export) ----------------

    def disk_breakdown(self) -> dict[str, list[tuple[str, float, float]]]:
        """Per-partition disk usage keyed by host entityId.

        Returns ``{host_id: [(drive_label, total_gb, used_gb), ...]}`` so the
        capacity report can list each partition (C:, D:, /…) on its own row —
        mirroring the Zabbix weekly report.

        One metrics query split by BOTH host and disk gives the host↔disk
        mapping directly from the data-point dimensions (no reliance on entity
        relationship shapes); disk display names come from a separate DISK
        entities lookup and fall back to the raw disk id. Fully best-effort:
        returns ``{}`` on any error or missing scope, and the export falls back
        to the summed disk total. Not called in mock mode.
        """
        if self.settings.mock_mode:
            return {}

        # 1) Per-(host, disk) used/avail bytes. Dimensions come back in splitBy
        #    order: [host_id, disk_id].
        used: dict[tuple[str, str], float] = {}
        avail: dict[tuple[str, str], float] = {}
        selector = ",".join(
            (
                'builtin:host.disk.used:splitBy("dt.entity.host","dt.entity.disk")',
                'builtin:host.disk.avail:splitBy("dt.entity.host","dt.entity.disk")',
            )
        )
        try:
            url = f"{self._base}/api/v2/metrics/query"
            params: dict[str, str] = {
                "metricSelector": selector,
                "from": "now-2h",
                "resolution": "Inf",
                "pageSize": "5000",
            }
            with self._client(headers=self._headers()) as client:
                while True:
                    resp = self._request_with_retries(client, "GET", url, params=params)
                    if resp.status_code == 403:
                        self.logger.warning(
                            "Dynatrace disk metrics 403 — token lacks metrics.read"
                        )
                        return {}
                    resp.raise_for_status()
                    payload = resp.json()
                    for series in payload.get("result", []):
                        mid = series.get("metricId", "")
                        for pt in series.get("data", []):
                            dims = pt.get("dimensions", [])
                            if len(dims) < 2:
                                continue
                            hid, did = dims[0], dims[1]
                            vals = [v for v in pt.get("values", []) if v is not None]
                            if not hid or not did or not vals:
                                continue
                            val = float(vals[-1])
                            if "disk.used" in mid:
                                used[(hid, did)] = val
                            elif "disk.avail" in mid:
                                avail[(hid, did)] = val
                    nxt = payload.get("nextPageKey")
                    if not nxt:
                        break
                    params = {"nextPageKey": nxt}
        except Exception as exc:  # noqa: BLE001 — contained; breakdown is optional
            self.logger.warning("Dynatrace disk metrics query failed: %s", exc)
            return {}
        if not used and not avail:
            self.logger.warning("Dynatrace disk metrics returned no data points")
            return {}

        # 2) Disk id -> display name (best-effort; raw id if unavailable).
        names: dict[str, str] = {}
        try:
            url = f"{self._base}/api/v2/entities"
            eparams: dict[str, str] = {
                "entitySelector": 'type("DISK")',
                "pageSize": "500",
            }
            with self._client(headers=self._headers()) as client:
                while url:
                    resp = self._request_with_retries(client, "GET", url, params=eparams)
                    if resp.status_code == 403:
                        break
                    resp.raise_for_status()
                    data = resp.json()
                    for e in data.get("entities", []):
                        if e.get("entityId"):
                            names[e["entityId"]] = e.get("displayName") or e["entityId"]
                    nxt = data.get("nextPageKey")
                    if nxt:
                        eparams = {"nextPageKey": nxt}
                    else:
                        url = ""
        except Exception as exc:  # noqa: BLE001 — names are cosmetic
            self.logger.warning("Dynatrace disk entities query failed: %s", exc)

        # 3) Join per (host, disk).
        out: dict[str, list[tuple[str, float, float]]] = {}
        for key in set(used) | set(avail):
            hid, did = key
            u = used.get(key, 0.0)
            total = u + avail.get(key, 0.0)
            if total <= 0:
                continue
            out.setdefault(hid, []).append(
                (names.get(did, did), round(total / _GB, 1), round(u / _GB, 1))
            )
        for hid in out:
            out[hid].sort()
        return out

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

    def collect_resolved_alerts(self) -> list[dict]:
        """Closed problems from the last ALERT_HISTORY_DAYS days (Problems v2).

        Same problemId as the live path, so a problem that was previously active
        is updated in place (marked resolved) rather than duplicated. A 403
        (no problems.read) degrades to an empty list, like collect_alerts.
        """
        if self.settings.mock_mode:
            return []
        days = max(1, int(self.settings.alert_history_days))
        url = f"{self._base}/api/v2/problems"
        params: dict[str, str] = {
            "problemSelector": 'status("CLOSED")',
            "from": f"now-{days}d",
            "pageSize": "500",
        }
        alerts: list[dict] = []
        with self._client(headers=self._headers()) as client:
            while True:
                resp = self._request_with_retries(client, "GET", url, params=params)
                if resp.status_code == 403:
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
                next_key = data.get("nextPageKey")
                if not next_key:
                    break
                params = {"nextPageKey": next_key}
        return alerts
