"""Runbook — the team's library of operational scripts, runnable from the UI.

Background: when an odd request comes in ("which of these 40 IPs is actually
monitored?", "give me every disabled host"), someone writes a one-off script.
Six months later nobody remembers it exists and it gets written again. The
Runbook is where those scripts live instead: each one is documented, findable,
and runnable by any admin without a shell.

Two design decisions are worth stating up front.

**Scripts are re-implemented here, not shelled out to.** The original ``.py``
files each opened their own API session, hardcoded a server list, and hardcoded
credentials. Running them as subprocesses would mean a fresh login per click,
credentials duplicated outside ``servers.yaml``, and no way to stream results
into the UI. Instead every report is a small function that borrows the
collector the scheduler already keeps authenticated — so a run is the API query
and nothing else.

**Read-only is enforced by the transport.** Every query goes through
:meth:`ZabbixCollector.read_rpc`, which refuses any method that is not a
``*.get``. Scripts that genuinely mutate the monitoring tools (creating hosts or
users, disabling monitoring) are catalogued here for discoverability but carry
``read_only=False`` and are never given a runner — the UI shows their
documentation and points at the CLI.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence

from app.config import Settings
from app.servers import load_servers

logger = logging.getLogger("runbook")

#: Author credit stamped onto every exported workbook.
SCRIPT_AUTHOR = "Eng. Ahmed Hussien"

#: Platform tabs offered by the Runbook filter.
PLATFORMS = ("zabbix", "dynatrace", "nnmi")


class RunbookError(Exception):
    """A script could not run (bad input, no instance, upstream failure)."""


# --- Registry types ---------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """One user-supplied input rendered as a form field on the script page."""

    name: str
    label: str
    kind: str = "text"  # text | textarea | datetime
    placeholder: str = ""
    required: bool = False
    help: str = ""


@dataclass(frozen=True)
class Script:
    """A catalogued operational script: what it does, and how to run it."""

    slug: str
    title: str
    platform: str
    #: One line shown in the script list.
    tagline: str
    #: Full prose description, one paragraph per entry.
    purpose: tuple[str, ...]
    #: Ordered "what happens when you press Run" steps.
    steps: tuple[str, ...]
    #: Column headers of the result table (also the export headers).
    columns: tuple[str, ...]
    #: API methods this script calls, shown so reviewers can audit it.
    api_calls: tuple[str, ...]
    read_only: bool = True
    params: tuple[Param, ...] = ()
    #: ``(collectors, params) -> rows``. None for documented-only scripts.
    runner: Callable[[list, dict[str, str]], list[list]] | None = None
    #: Extra caveats worth surfacing (version drift, cost, etc.).
    notes: tuple[str, ...] = ()
    author: str = SCRIPT_AUTHOR


# --- Small helpers ----------------------------------------------------------


def _iface_ip(interfaces: Sequence[dict]) -> str:
    """Pick the display IP: the main interface, else the first one.

    A DNS-configured interface (``useip == "0"``) shows its name instead, which
    is what an operator actually needs to see.
    """
    if not interfaces:
        return ""
    main = next((i for i in interfaces if str(i.get("main")) == "1"), interfaces[0])
    if str(main.get("useip", "1")) == "0" and main.get("dns"):
        return str(main["dns"])
    return str(main.get("ip") or main.get("dns") or "")


def _names(items: Sequence[dict], key: str = "name") -> str:
    """Comma-join a list of ``{name: ...}`` dicts, sorted and deduped."""
    return ", ".join(sorted({str(i.get(key, "")).strip() for i in items if i.get(key)}))


def _groups(host: dict) -> str:
    """Host groups, tolerating both the 6.0+ and legacy response keys."""
    return _names(host.get("hostgroups") or host.get("groups") or [])


def _status(host: dict) -> str:
    return "Disabled" if str(host.get("status")) == "1" else "Enabled"


def _split_list(raw: str) -> list[str]:
    """Split a free-text box of IPs/names on commas, semicolons or whitespace."""
    return [tok for tok in re.split(r"[\s,;]+", (raw or "").strip()) if tok]


def _parse_when(raw: str, fallback: datetime) -> datetime:
    """Parse a datetime-local / ISO string as UTC, falling back on failure."""
    v = (raw or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return fallback


def _require_zabbix(collectors: list) -> list:
    """Narrow the given collectors to live Zabbix ones, or explain why not."""
    zbx = [c for c in collectors if getattr(c, "name", "") == "zabbix"]
    if not zbx:
        raise RunbookError(
            "No Zabbix instance selected. Pick one from the instance list, or "
            "add a Zabbix server to servers.yaml."
        )
    if getattr(zbx[0].settings, "mock_mode", False):
        raise RunbookError(
            "MOCK_MODE is on, so there is no real Zabbix to query. "
            "Set MOCK_MODE=false and configure servers.yaml to use the Runbook."
        )
    return zbx


def _host_query(collector, params: dict) -> list[dict]:
    """``host.get`` with the fields every host-shaped report needs."""
    base = {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["ip", "dns", "useip", "main", "available", "error"],
        "selectHostGroups": ["name"],
        "selectParentTemplates": ["name"],
    }
    base.update(params)
    return list(collector.read_rpc("host.get", base) or [])


# --- Runners ----------------------------------------------------------------


def run_unavailable_hosts(collectors: list, params: dict[str, str]) -> list[list]:
    """Hosts Zabbix currently cannot reach, read from the INTERFACE."""
    rows: list[list] = []
    for c in _require_zabbix(collectors):
        for h in _host_query(c, {"filter": {"status": "0"}}):
            # Zabbix 6.0 moved availability from the host onto each interface,
            # which is why the original scripts' host-level `available: 2`
            # filter quietly returned nothing on newer servers. Checking the
            # interfaces works on every version.
            bad = [i for i in h.get("interfaces", []) if str(i.get("available")) == "2"]
            if not bad:
                continue
            rows.append([
                c.instance,
                h.get("host", ""),
                h.get("name", ""),
                _iface_ip(h.get("interfaces", [])),
                _groups(h),
                _names(h.get("parentTemplates") or []),
                (bad[0].get("error") or "").strip()[:200],
            ])
    rows.sort(key=lambda r: (r[0], r[1].lower()))
    return rows


def run_disabled_hosts(collectors: list, params: dict[str, str]) -> list[list]:
    """Every host with monitoring switched off (``status = 1``)."""
    rows: list[list] = []
    for c in _require_zabbix(collectors):
        for h in _host_query(c, {"filter": {"status": "1"}}):
            rows.append([
                c.instance,
                h.get("hostid", ""),
                h.get("host", ""),
                h.get("name", ""),
                _iface_ip(h.get("interfaces", [])),
                _groups(h),
                _names(h.get("parentTemplates") or []),
                "Disabled",
            ])
    rows.sort(key=lambda r: (r[0], r[2].lower()))
    return rows


def run_host_backup(collectors: list, params: dict[str, str]) -> list[list]:
    """Full host inventory snapshot — the 'backup before you change anything' report."""
    rows: list[list] = []
    for c in _require_zabbix(collectors):
        hosts = c.read_rpc("host.get", {
            "output": ["hostid", "host", "name", "status", "inventory_mode"],
            "selectInterfaces": ["ip", "dns", "useip", "main"],
            "selectHostGroups": ["name"],
            "selectParentTemplates": ["name"],
            "selectTags": "extend",
        }) or []
        for h in hosts:
            tags = "; ".join(
                f"{t.get('tag')}={t['value']}" if t.get("value") else str(t.get("tag", ""))
                for t in h.get("tags", [])
            )
            mode = {"-1": "Disabled", "0": "Manual", "1": "Automatic"}.get(
                str(h.get("inventory_mode")), str(h.get("inventory_mode", ""))
            )
            rows.append([
                c.instance,
                h.get("hostid", ""),
                h.get("host", ""),
                h.get("name", ""),
                _status(h),
                _iface_ip(h.get("interfaces", [])),
                _groups(h),
                _names(h.get("parentTemplates") or []),
                tags,
                mode,
            ])
    rows.sort(key=lambda r: (r[0], r[2].lower()))
    return rows


def run_ip_lookup(collectors: list, params: dict[str, str]) -> list[list]:
    """Answer 'where is this IP monitored?' across every selected instance."""
    ips = _split_list(params.get("ips", ""))
    if not ips:
        raise RunbookError("Enter at least one IP address.")
    rows: list[list] = []
    for c in _require_zabbix(collectors):
        # Filter server-side. The original scripts downloaded every host and
        # matched in Python, which is the single slowest thing they did.
        for h in _host_query(c, {"filter": {"ip": ips}}):
            matched = sorted({
                str(i.get("ip")) for i in h.get("interfaces", []) if i.get("ip") in ips
            })
            rows.append([
                ", ".join(matched),
                c.instance,
                h.get("hostid", ""),
                h.get("host", ""),
                h.get("name", ""),
                _status(h),
                _groups(h),
                _names(h.get("parentTemplates") or []),
            ])
    found = {ip for r in rows for ip in _split_list(r[0])}
    for ip in ips:
        if ip not in found:
            rows.append([ip, "—", "", "", "", "NOT FOUND", "", ""])
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def run_ip_monitoring_status(collectors: list, params: dict[str, str]) -> list[list]:
    """For a list of IPs: is it in Zabbix, is it enabled, and is it collecting?"""
    ips = _split_list(params.get("ips", ""))
    if not ips:
        raise RunbookError("Enter at least one IP address.")
    rows: list[list] = []
    seen: set[str] = set()
    for c in _require_zabbix(collectors):
        hosts = _host_query(c, {"filter": {"ip": ips}})
        if not hosts:
            continue
        # One item.get for ALL matched hosts, counted in Python. The original
        # issued a separate counting call per host.
        host_ids = [h["hostid"] for h in hosts if h.get("hostid")]
        counts: Counter[str] = Counter()
        for chunk in (host_ids[i:i + 200] for i in range(0, len(host_ids), 200)):
            for it in c.read_rpc("item.get", {
                "output": ["hostid"], "hostids": chunk, "filter": {"status": "0"},
            }) or []:
                counts[str(it.get("hostid"))] += 1

        for h in hosts:
            hid = str(h.get("hostid", ""))
            n_items = counts.get(hid, 0)
            enabled = str(h.get("status")) != "1"
            matched = sorted({
                str(i.get("ip")) for i in h.get("interfaces", []) if i.get("ip") in ips
            })
            seen.update(matched)
            rows.append([
                ", ".join(matched),
                c.instance,
                hid,
                h.get("host", ""),
                h.get("name", ""),
                _status(h),
                "MONITORED" if (enabled and n_items) else "NOT MONITORED",
                n_items,
                _names(h.get("parentTemplates") or []),
            ])
    # Keep the report one-to-one with the input list.
    for ip in ips:
        if ip not in seen:
            rows.append([ip, "—", "", "", "", "NOT FOUND", "NOT MONITORED", 0, ""])
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def run_proxy_status(collectors: list, params: dict[str, str]) -> list[list]:
    """Proxy fleet health, tolerating the 6.4 proxy schema rename."""
    rows: list[list] = []
    now = datetime.now(timezone.utc)
    for c in _require_zabbix(collectors):
        try:
            proxies = c.read_rpc("proxy.get", {
                "output": ["proxyid", "name", "state", "address", "port",
                           "operating_mode", "lastaccess", "version"],
                "selectHosts": "count",
            }) or []
        except Exception:  # noqa: BLE001 — fall back to the pre-6.4 schema
            proxies = c.read_rpc("proxy.get", {
                "output": "extend", "selectHosts": "count",
            }) or []

        for p in proxies:
            # 6.4+: name/state/operating_mode. Older: host/status (5=active,
            # 6=passive) and no state field at all — availability is inferred
            # from how long ago the proxy last checked in.
            name = p.get("name") or p.get("host") or ""
            last = int(p.get("lastaccess") or 0)
            seen_at = datetime.fromtimestamp(last, timezone.utc) if last else None
            if "state" in p:
                state = {"1": "OFFLINE", "2": "ONLINE"}.get(str(p["state"]), "UNKNOWN")
            elif seen_at is not None:
                state = "ONLINE" if (now - seen_at).total_seconds() < 300 else "OFFLINE"
            else:
                state = "UNKNOWN"
            mode_raw = str(p.get("operating_mode", p.get("status", "")))
            mode = {"0": "ACTIVE", "1": "PASSIVE", "5": "ACTIVE", "6": "PASSIVE"}.get(
                mode_raw, "UNKNOWN"
            )
            rows.append([
                c.instance,
                p.get("proxyid", ""),
                name,
                state,
                p.get("address", ""),
                p.get("port", ""),
                mode,
                seen_at.replace(tzinfo=None) if seen_at else None,
                p.get("version", ""),
                int(p.get("hosts", 0) or 0),
            ])
    rows.sort(key=lambda r: (r[0], str(r[2]).lower()))
    return rows


def run_group_audit(collectors: list, params: dict[str, str]) -> list[list]:
    """Per-host-group audit: enabled, and actually collecting items?

    Written for the vCenter groups (``Vcenter_*``) but the pattern applies to
    any group — an enabled host with zero items looks healthy and monitors
    nothing.
    """
    pattern = (params.get("group") or "Vcenter_").strip()
    rows: list[list] = []
    for c in _require_zabbix(collectors):
        groups = c.read_rpc("hostgroup.get", {
            "output": ["groupid", "name"], "search": {"name": pattern},
        }) or []
        if not groups:
            continue
        by_id = {str(g["groupid"]): g["name"] for g in groups}
        hosts = c.read_rpc("host.get", {
            "output": ["hostid", "host", "name", "status"],
            "groupids": list(by_id),
            "selectHostGroups": ["groupid", "name"],
            "selectParentTemplates": ["name"],
        }) or []
        # One counting pass for every host in every matched group.
        host_ids = [h["hostid"] for h in hosts if h.get("hostid")]
        counts: Counter[str] = Counter()
        for chunk in (host_ids[i:i + 200] for i in range(0, len(host_ids), 200)):
            for it in c.read_rpc("item.get", {
                "output": ["hostid"], "hostids": chunk, "filter": {"status": "0"},
            }) or []:
                counts[str(it.get("hostid"))] += 1

        for h in hosts:
            hid = str(h.get("hostid", ""))
            n_items = counts.get(hid, 0)
            enabled = str(h.get("status")) != "1"
            mine = [
                g["name"]
                for g in (h.get("hostgroups") or h.get("groups") or [])
                if str(g.get("groupid")) in by_id
            ]
            rows.append([
                c.instance,
                ", ".join(sorted(mine)),
                hid,
                h.get("host", ""),
                h.get("name", ""),
                _status(h),
                "MONITORED" if (enabled and n_items) else "NOT MONITORED",
                n_items,
                _names(h.get("parentTemplates") or []),
            ])
    if not rows:
        raise RunbookError(f"No host group matched {pattern!r} on the selected instance(s).")
    rows.sort(key=lambda r: (r[0], r[1], r[3].lower()))
    return rows


def run_ip_history(collectors: list, params: dict[str, str]) -> list[list]:
    """Raw numeric metric history for one IP over a time window."""
    ips = _split_list(params.get("ips", ""))
    if len(ips) != 1:
        raise RunbookError("Enter exactly one IP address for the history report.")
    ip = ips[0]
    now = datetime.now(timezone.utc)
    start = _parse_when(params.get("date_from", ""), now.replace(hour=0, minute=0))
    end = _parse_when(params.get("date_to", ""), now)
    if end <= start:
        raise RunbookError("The 'To' time must be later than the 'From' time.")

    zbx = _require_zabbix(collectors)
    limit = int(getattr(zbx[0].settings, "runbook_max_rows", 20000))
    rows: list[list] = []
    for c in zbx:
        for h in _host_query(c, {"filter": {"ip": [ip]}}):
            items = c.read_rpc("item.get", {
                "output": ["itemid", "name", "key_", "units", "value_type"],
                "hostids": h["hostid"],
                "filter": {"status": "0"},
            }) or []
            # history.get takes ONE value_type per call, so group the items and
            # issue one call per type rather than one per item.
            by_type: dict[str, list[dict]] = {}
            for it in items:
                vt = str(it.get("value_type"))
                if vt in ("0", "3"):  # float / unsigned only
                    by_type.setdefault(vt, []).append(it)

            for vt, group in by_type.items():
                meta = {str(i["itemid"]): i for i in group}
                points = c.read_rpc("history.get", {
                    "output": "extend",
                    "history": int(vt),
                    "itemids": list(meta),
                    "time_from": int(start.timestamp()),
                    "time_till": int(end.timestamp()),
                    "sortfield": "clock",
                    "sortorder": "ASC",
                    "limit": max(0, limit - len(rows)),
                }) or []
                for p in points:
                    it = meta.get(str(p.get("itemid")), {})
                    rows.append([
                        c.instance,
                        h.get("host", ""),
                        ip,
                        p.get("itemid", ""),
                        it.get("name", ""),
                        it.get("key_", ""),
                        it.get("units", ""),
                        datetime.fromtimestamp(int(p.get("clock", 0)), timezone.utc)
                        .replace(tzinfo=None),
                        p.get("value", ""),
                    ])
                if len(rows) >= limit:
                    logger.warning("ip history truncated at %d rows", limit)
                    return rows[:limit]
    if not rows:
        raise RunbookError(
            f"No numeric history found for {ip} in that window. "
            "Check the IP is monitored and the range covers collected data."
        )
    return rows


# --- The catalogue ----------------------------------------------------------

_IPS_PARAM = Param(
    name="ips",
    label="IP addresses",
    kind="textarea",
    placeholder="10.19.42.67\n10.19.42.82\n10.19.87.12",
    required=True,
    help="One per line, or separated by commas / spaces.",
)

SCRIPTS: tuple[Script, ...] = (
    Script(
        slug="unavailable-hosts",
        title="Unavailable Hosts",
        platform="zabbix",
        tagline="Enabled hosts that Zabbix currently cannot reach.",
        purpose=(
            "Lists every host that is switched on in Zabbix but whose monitoring "
            "interface is reporting as unavailable — the agent is not answering, "
            "SNMP is timing out, or the host is unreachable. These are the hosts "
            "that look monitored on paper but are silently producing nothing.",
            "Disabled hosts are deliberately excluded: a host you turned off on "
            "purpose is not a problem, and mixing the two makes the list useless.",
        ),
        steps=(
            "Query every enabled host on the selected instance, pulling its "
            "interfaces, host groups and linked templates in one call.",
            "Keep only hosts with at least one interface in the 'unavailable' state.",
            "Report the failing interface's own error text, which is usually the "
            "fastest way to tell a dead agent from a firewall block.",
        ),
        columns=("Zabbix", "Host", "Visible Name", "IP", "Host Groups",
                 "Templates", "Last Error"),
        api_calls=("host.get",),
        runner=run_unavailable_hosts,
        notes=(
            "Availability moved from the host object onto the interface in "
            "Zabbix 6.0. This reads the interface, so it works on 5.x and 6.x/7.x "
            "alike — the original per-server scripts filtered on the old host-level "
            "field and returned nothing on newer servers.",
        ),
    ),
    Script(
        slug="disabled-hosts",
        title="Disabled Hosts",
        platform="zabbix",
        tagline="Everything with monitoring switched off, and what it was linked to.",
        purpose=(
            "Returns every host whose status is Disabled, together with its IP, "
            "host groups and the templates it still has attached.",
            "The usual reason to run it is an audit: hosts get disabled during a "
            "migration or a maintenance window and are never switched back on. "
            "Because the template list comes along, you can also see at a glance "
            "what monitoring would resume if you re-enabled each one.",
        ),
        steps=(
            "Query hosts filtered on status = 1 (disabled) on each selected instance.",
            "Resolve the display IP from the main interface, falling back to DNS "
            "name for interfaces configured by name.",
            "Flatten groups and templates into sorted, comma-joined columns.",
        ),
        columns=("Zabbix", "Host ID", "Host", "Visible Name", "IP",
                 "Host Groups", "Templates", "Status"),
        api_calls=("host.get",),
        runner=run_disabled_hosts,
    ),
    Script(
        slug="host-inventory-backup",
        title="Host Inventory Backup",
        platform="zabbix",
        tagline="Full snapshot of every host — take it before you change anything.",
        purpose=(
            "Produces a complete inventory of one Zabbix server: every host with "
            "its technical and visible name, enabled/disabled status, IP, host "
            "groups, linked templates, tags and inventory mode.",
            "This is the report to export before a bulk change, a template "
            "re-link or a server upgrade. If something is wrong afterwards, the "
            "workbook tells you exactly what the configuration looked like before.",
        ),
        steps=(
            "Pull all hosts in a single call, including interfaces, groups, "
            "templates and tags.",
            "Render tags as 'tag=value' pairs joined by semicolons, and translate "
            "the numeric inventory mode into Disabled / Manual / Automatic.",
            "Emit one row per host, sorted by instance then host name.",
        ),
        columns=("Zabbix", "Host ID", "Host Name", "Visible Name", "Status",
                 "IP Address", "Host Groups", "Assigned Templates", "Tags",
                 "Inventory Mode"),
        api_calls=("host.get",),
        runner=run_host_backup,
        notes=(
            "This is the heaviest read in the Runbook — it returns every host on "
            "the instance. Pick a single instance rather than 'All' unless you "
            "really want the whole estate in one sheet.",
        ),
    ),
    Script(
        slug="ip-lookup",
        title="IP Lookup",
        platform="zabbix",
        tagline="Given an IP, find which Zabbix server monitors it — and as what.",
        purpose=(
            "Answers the question that comes up most often on a shift: somebody "
            "sends an IP and asks whether it is monitored, and if so where. The "
            "script searches every selected Zabbix instance for a host owning "
            "that IP on any interface.",
            "Each hit reports the instance it was found on, the host and visible "
            "names, whether it is enabled, its host groups and its templates. IPs "
            "that match nothing still get a row marked NOT FOUND, so the output "
            "lines up one-to-one with the list you pasted in.",
        ),
        steps=(
            "Split the input box into individual IPs (commas, spaces or newlines).",
            "Ask each instance for hosts filtered on those IPs server-side.",
            "Report every match, then add a NOT FOUND row for any IP nothing matched.",
        ),
        columns=("IP", "Zabbix", "Host ID", "Host", "Visible Name", "Status",
                 "Host Groups", "Templates"),
        api_calls=("host.get",),
        params=(_IPS_PARAM,),
        runner=run_ip_lookup,
        notes=(
            "The filtering happens on the Zabbix server, not here. The original "
            "scripts downloaded the full host list and matched in Python, which "
            "is why they took so long on the larger instances.",
        ),
    ),
    Script(
        slug="ip-monitoring-status",
        title="IP Monitoring Status",
        platform="zabbix",
        tagline="Bulk check: are these IPs in Zabbix, enabled, and collecting data?",
        purpose=(
            "Takes a list of IPs and produces the audit table the capacity and "
            "network teams usually ask for: for each address, is there a host, "
            "is it enabled, how many items does it have, and which templates.",
            "The key column is Monitoring. A host can be enabled and still "
            "collect nothing — no template linked, or every item disabled. This "
            "report marks that case NOT MONITORED rather than letting an enabled "
            "but empty host pass as healthy.",
        ),
        steps=(
            "Look each IP up server-side on every selected instance.",
            "Count the enabled items of all matched hosts in one batched call.",
            "Mark a host MONITORED only when it is enabled AND has at least one "
            "enabled item; everything else, including unmatched IPs, is "
            "NOT MONITORED.",
        ),
        columns=("IP", "Zabbix", "Host ID", "Host Name", "Visible Name",
                 "Status", "Monitoring", "Items", "Templates"),
        api_calls=("host.get", "item.get"),
        params=(_IPS_PARAM,),
        runner=run_ip_monitoring_status,
    ),
    Script(
        slug="proxy-status",
        title="Proxy Status",
        platform="zabbix",
        tagline="Health of every Zabbix proxy, with how many hosts each one carries.",
        purpose=(
            "Reports the whole proxy fleet across the selected instances: each "
            "proxy's online/offline state, its address and port, whether it runs "
            "in active or passive mode, when it last checked in, its version, and "
            "how many hosts depend on it.",
            "An offline proxy silently stops data for every host behind it, so "
            "the host count column matters as much as the status — it tells you "
            "how big the blind spot is.",
        ),
        steps=(
            "Call proxy.get on each selected instance, asking for a host count.",
            "Translate the numeric state and operating mode into readable "
            "ONLINE/OFFLINE and ACTIVE/PASSIVE values.",
            "Convert the last-access Unix timestamp into a real datetime.",
        ),
        columns=("Zabbix", "Proxy ID", "Proxy Name", "Status", "Proxy Address",
                 "Port", "Proxy Mode", "Last Access", "Version", "Hosts"),
        api_calls=("proxy.get",),
        runner=run_proxy_status,
        notes=(
            "Zabbix 6.4 renamed the proxy fields (host -> name, status -> "
            "operating_mode) and added a separate state field. Both schemas are "
            "handled, so this works against old and new servers in one run.",
        ),
    ),
    Script(
        slug="group-audit",
        title="Host Group Audit (vCenter)",
        platform="zabbix",
        tagline="Per-group audit: which hosts are enabled but collecting nothing.",
        purpose=(
            "Walks every host group whose name matches the search text — "
            "'Vcenter_' by default, which covers the vCenter groups — and reports "
            "each host in them with its status, item count and templates.",
            "It was written for the VMware estate, where a guest can be present "
            "and enabled but have no working template link, so it shows up as "
            "monitored while producing no data. Change the search text to audit "
            "any other family of groups the same way.",
        ),
        steps=(
            "Find every host group whose name contains the search text.",
            "List all hosts in those groups with their groups and templates.",
            "Count enabled items for all of them in one batched call, then mark "
            "each host MONITORED or NOT MONITORED.",
        ),
        columns=("Zabbix", "Host Group", "Host ID", "Host Name", "Visible Name",
                 "Status", "Monitoring", "Items", "Templates"),
        api_calls=("hostgroup.get", "host.get", "item.get"),
        params=(
            Param(
                name="group",
                label="Host group contains",
                placeholder="Vcenter_",
                help="Substring match on the group name. Leave as Vcenter_ for "
                     "the VMware audit.",
            ),
        ),
        runner=run_group_audit,
    ),
    Script(
        slug="ip-history",
        title="IP Metric History",
        platform="zabbix",
        tagline="Raw numeric history for one IP over a chosen time window.",
        purpose=(
            "Exports every numeric sample Zabbix stored for a single host, "
            "between the two timestamps you choose. Each row is one reading: the "
            "metric name, its key, its unit, the timestamp and the value.",
            "This is the report to run when someone asks what a server was doing "
            "during an incident. Only float and unsigned items are included — "
            "text and log items are not comparable and would just bloat the sheet.",
        ),
        steps=(
            "Find the host owning the IP on each selected instance.",
            "List its enabled numeric items, then fetch history one call per "
            "value type rather than one call per item.",
            "Convert Unix clock values into datetimes and sort oldest first.",
        ),
        columns=("Zabbix", "Host", "IP", "Item ID", "Metric", "Key", "Units",
                 "Timestamp", "Value"),
        api_calls=("host.get", "item.get", "history.get"),
        params=(
            Param(name="ips", label="IP address", placeholder="10.19.42.67",
                  required=True, help="Exactly one IP for this report."),
            Param(name="date_from", label="From (UTC)", kind="datetime", required=True),
            Param(name="date_to", label="To (UTC)", kind="datetime", required=True),
        ),
        runner=run_ip_history,
        notes=(
            "History is the largest table in any Zabbix database. Results are "
            "capped by RUNBOOK_MAX_ROWS; keep the window tight — a day or two "
            "of a busy host is already tens of thousands of rows.",
        ),
    ),
    # --- Documented, deliberately not runnable from the web -----------------
    Script(
        slug="add-hosts",
        title="Bulk Add Hosts",
        platform="zabbix",
        tagline="Creates hosts in Zabbix from a spreadsheet. CLI only.",
        purpose=(
            "Reads a spreadsheet with IP, Hostname, Group and Port columns and "
            "creates one Zabbix host per row. Port 161 produces an SNMPv2 "
            "interface using the {$SNMP_COMMUNITY} macro with bulk requests "
            "enabled; ports 9999 and 10050 produce a Zabbix Agent interface. Any "
            "other port is rejected.",
            "Rows whose host name already exists are skipped rather than "
            "duplicated, and the host group must already exist — the script "
            "resolves the name to an ID and fails the row if there is no match.",
        ),
        steps=(
            "Validate every row: IP, Hostname, Group and an allowed port are all "
            "required.",
            "Skip any host name that already exists on the target server.",
            "Create the host with a single main interface matching the port.",
        ),
        columns=("IP", "Hostname", "Group", "Port", "Result"),
        api_calls=("host.get", "hostgroup.get", "host.create"),
        read_only=False,
        notes=(
            "This script CREATES objects in Zabbix, so it has no Run button "
            "here. The Runbook is a reporting surface: everything it can run "
            "goes through a transport that refuses any non-read API method. Run "
            "this one from the CLI, where the spreadsheet and the target server "
            "are explicit and reviewable.",
        ),
    ),
    Script(
        slug="add-users",
        title="Bulk Add Users",
        platform="zabbix",
        tagline="Creates LDAP/SAML users from a spreadsheet. CLI only.",
        purpose=(
            "Reads a spreadsheet with Username, Groups and Role columns and "
            "creates one Zabbix user per row, splitting the Groups cell on "
            "semicolons for multi-group membership.",
            "No password is sent when the account is created, which is "
            "deliberate: these users authenticate against LDAP/SAML, and a "
            "locally-set password would be both unused and a liability.",
        ),
        steps=(
            "Skip any username that already exists on the target server.",
            "Resolve each user-group name to its ID and the role name to its ID.",
            "Create the user with that role and group list, and no password.",
        ),
        columns=("Username", "Groups", "Role", "Result"),
        api_calls=("user.get", "usergroup.get", "role.get", "user.create"),
        read_only=False,
        notes=(
            "This script CREATES users, so it has no Run button here. Creating "
            "accounts is an identity change and belongs in a reviewed CLI run, "
            "not behind a dashboard button.",
        ),
    ),
    Script(
        slug="disable-hosts",
        title="Bulk Disable Hosts",
        platform="zabbix",
        tagline="Switches monitoring off for a list of IPs. CLI only.",
        purpose=(
            "Takes a list of IP addresses, finds every Zabbix host owning one of "
            "them, and sets each to disabled so Zabbix stops monitoring it. IPs "
            "with no matching host are reported as Not Found and skipped.",
            "The normal use is decommissioning: a batch of servers is retired and "
            "their alerts need to stop before the hosts themselves are removed.",
        ),
        steps=(
            "Look each IP up on the target server.",
            "Set status = 1 on every matching host.",
            "Report each IP as Disabled, Failed or Not Found.",
        ),
        columns=("IP", "Host ID", "Host", "Result"),
        api_calls=("host.get", "host.update"),
        read_only=False,
        notes=(
            "This script STOPS monitoring, which means alerts stop too. It has "
            "no Run button here on purpose — a mistyped IP list silently blinds "
            "you to real servers. Use 'IP Lookup' first to confirm exactly which "
            "hosts the list resolves to, then run the change from the CLI.",
        ),
    ),
)

SCRIPTS_BY_SLUG: dict[str, Script] = {s.slug: s for s in SCRIPTS}


def scripts_for(platform: str | None) -> list[Script]:
    """Return the catalogue, optionally narrowed to one platform."""
    if not platform or platform == "all":
        return list(SCRIPTS)
    return [s for s in SCRIPTS if s.platform == platform]


def platform_counts() -> dict[str, int]:
    """``{platform: number of catalogued scripts}`` for the filter tabs."""
    counts = {p: 0 for p in PLATFORMS}
    for s in SCRIPTS:
        counts[s.platform] = counts.get(s.platform, 0) + 1
    return counts


def runbook_instances(settings: Settings, platform: str | None = None) -> list[dict]:
    """Configured instances the Runbook can target, for the instance picker."""
    return [
        {"name": s.name, "platform": s.platform}
        for s in load_servers(settings)
        if not platform or platform == "all" or s.platform == platform
    ]


def collectors_for(instance: str | None, platform: str) -> list:
    """Resolve the live collector(s) a run should use.

    Borrowing the scheduler's already-authenticated collectors is what makes a
    run cheap — there is no second login and no second connection pool.
    """
    from app.scheduler import get_service

    service = get_service()
    if instance and instance != "all":
        collector = service.get(instance)
        if collector is None:
            raise RunbookError(f"Unknown instance {instance!r}.")
        return [collector]
    return [c for c in service.collectors.values() if getattr(c, "name", "") == platform]


def execute(
    script: Script,
    instance: str | None,
    params: dict[str, str],
    settings: Settings,
) -> list[list]:
    """Run a catalogued script and return its rows, capped and validated."""
    if script.runner is None or not script.read_only:
        raise RunbookError(
            f"{script.title} changes the monitoring configuration and cannot be "
            "run from the Runbook. See the notes on this page."
        )
    if not settings.enable_runbook:
        raise RunbookError("The Runbook is disabled (ENABLE_RUNBOOK=false).")

    for p in script.params:
        if p.required and not (params.get(p.name) or "").strip():
            raise RunbookError(f"{p.label} is required.")

    collectors = collectors_for(instance, script.platform)
    rows = script.runner(collectors, params)

    cap = max(1, int(settings.runbook_max_rows))
    if len(rows) > cap:
        logger.warning("%s returned %d rows; capped at %d", script.slug, len(rows), cap)
        rows = rows[:cap]
    return rows
