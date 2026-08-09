"""NNMi collector using the SOAP web services.

Nodes come from the NodeBean service, open incidents from the IncidentBean
service. We use raw SOAP envelopes over httpx + lxml rather than zeep: NNMi's
WSDLs are large and often fight zeep's schema parsing, and hand-built envelopes
keep the dependency surface small and predictable.

The envelope shape (a ``filt:expression`` ``arg0`` carrying ``offset`` /
``maxObjects`` / a condition) and the ``*.sdk.nms.ov.hp.com`` namespaces match
the NNMi SDK web-service contract; the services live at the server root (not the
``/nnm`` console path), with an empty ``SOAPAction`` header.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from lxml import etree

from app.collectors import mock_data
from app.collectors.base import BaseCollector, CollectorError
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import nnmi_severity_label, normalize_nnmi_severity
from app.servers import ServerConfig

# NNMi status string -> normalized host status.
_NNMI_STATUS = {
    "NORMAL": HostStatus.up,
    "WARNING": HostStatus.up,
    "MINOR": HostStatus.up,
    "MAJOR": HostStatus.down,
    "CRITICAL": HostStatus.down,
    "DISABLED": HostStatus.unknown,
    "UNKNOWN": HostStatus.unknown,
    "NO_STATUS": HostStatus.unknown,
}

_NODE_NS = "http://node.sdk.nms.ov.hp.com/"
_INCIDENT_NS = "http://incident.sdk.nms.ov.hp.com/"
_IPADDR_NS = "http://ipaddress.sdk.nms.ov.hp.com/"

_PAGE_SIZE = 500
_MAX_PAGES = 40  # safety cap (up to 20k objects/entity)
# IPAddressBean can be much larger than the node table (many IPs per node), so
# allow deeper paging when filling in missing node IPs — bounded, and we
# early-exit as soon as every wanted node has an address.
_IP_MAX_PAGES = 400
_TIMEOUT = 120.0

# SOAP request envelope: an AND expression carrying paging constraints and one
# condition (NNMi SDK filter grammar).
_SOAP_TEMPLATE = (
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:svc="{ns}">'
    "<soapenv:Header/>"
    "<soapenv:Body>"
    "<svc:{operation}>"
    '<arg0 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xmlns:filt="http://filter.sdk.nms.ov.hp.com/" xsi:type="filt:expression">'
    "<operator>AND</operator>"
    '<subFilters xsi:type="filt:constraint">'
    "<name>offset</name><value>{offset}</value></subFilters>"
    '<subFilters xsi:type="filt:constraint">'
    "<name>maxObjects</name><value>{max_objects}</value></subFilters>"
    '<subFilters xsi:type="filt:condition">'
    "<name>{cond_field}</name><operator>{cond_op}</operator>"
    "<value>{cond_value}</value></subFilters>"
    "</arg0>"
    "</svc:{operation}>"
    "</soapenv:Body>"
    "</soapenv:Envelope>"
)


class NnmiCollector(BaseCollector):
    """Collects nodes and open incidents from one NNMi instance via SOAP."""

    name = "nnmi"
    platform = SourcePlatform.nnmi

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        super().__init__(config, settings)
        base = config.url.rstrip("/")
        # SOAP web services live at the server root, NOT under the "/nnm"
        # console webapp path; strip it so we don't get a 403.
        for suffix in ("/nnm", "/nnmi"):
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)]
                break
        self._base = base

    # --- SOAP plumbing ------------------------------------------------------

    def _soap_post(self, path: str, envelope: str) -> bytes:
        """POST a SOAP envelope to ``base+path`` and return the raw response."""
        url = self._base + path
        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "SOAPAction": "",
        }
        auth = (self.config.user, self.config.password)
        with self._client(timeout=_TIMEOUT, headers=headers, auth=auth) as client:
            resp = self._request_with_retries(
                client, "POST", url, content=envelope.encode("utf-8")
            )
        if resp.status_code == 403:
            raise CollectorError(
                f"403 Forbidden from {url}. Ensure the NNMi account "
                f"'{self.config.user}' has the 'Web Service Clients' role."
            )
        if resp.status_code == 401:
            raise CollectorError(f"401 Unauthorized from {url} — check credentials.")
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _parse_items(xml_bytes: bytes) -> list[dict]:
        """Parse ``<item>`` elements into dicts of their simple children.

        Nested structures (children that themselves have element children, e.g.
        capabilities) are skipped, matching the NNMi SDK export convention.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as exc:  # pragma: no cover - defensive
            raise CollectorError(f"invalid SOAP response: {exc}") from exc

        # Surface SOAP faults as clear errors instead of silent empties.
        faults = root.xpath("//*[local-name()='Fault']")
        if faults:
            strings = faults[0].xpath(".//*[local-name()='faultstring']/text()")
            msg = strings[0] if strings else "SOAP fault"
            raise CollectorError(f"NNMi SOAP fault: {msg}")

        items: list[dict] = []
        for el in root.iter():
            if etree.QName(el).localname != "item":
                continue
            # Skip <item> elements nested inside another <item> (e.g. the
            # per-node capabilities / customAttributes lists).
            ancestor = el.getparent()
            nested = False
            while ancestor is not None:
                if etree.QName(ancestor).localname == "item":
                    nested = True
                    break
                ancestor = ancestor.getparent()
            if nested:
                continue
            record: dict[str, str] = {}
            for child in el:
                if len(child):  # nested element -> skip
                    continue
                record[etree.QName(child).localname] = (child.text or "").strip()
            if record:
                items.append(record)
        return items

    def _iter_pages(
        self,
        path: str,
        ns: str,
        operation: str,
        cond: tuple[str, str, str],
        max_pages: int = _MAX_PAGES,
    ):
        """Yield successive pages (lists of item dicts) for one entity/condition."""
        offset = 0
        for _ in range(max_pages):
            envelope = _SOAP_TEMPLATE.format(
                ns=ns,
                operation=operation,
                offset=offset,
                max_objects=_PAGE_SIZE,
                cond_field=cond[0],
                cond_op=cond[1],
                cond_value=cond[2],
            )
            page = self._parse_items(self._soap_post(path, envelope))
            yield page
            if len(page) < _PAGE_SIZE:
                return
            offset += _PAGE_SIZE

    def _fetch(
        self,
        path: str,
        ns: str,
        operation: str,
        cond: tuple[str, str, str],
    ) -> list[dict]:
        """Fetch all pages for one entity/condition."""
        rows: list[dict] = []
        for page in self._iter_pages(path, ns, operation, cond):
            rows.extend(page)
        return rows

    def _fetch_with_fallback(
        self, path: str, ns: str, operation: str
    ) -> list[dict]:
        """Fetch with the usual name-LIKE-% filter, falling back to id GE 0."""
        for cond in (("name", "LIKE", "%"), ("id", "GE", "0")):
            rows = self._fetch(path, ns, operation, cond)
            if rows:
                return rows
        return []

    @staticmethod
    def _first(record: dict, keys: tuple[str, ...]) -> str | None:
        """Return the first non-empty value among ``keys`` (case-insensitive)."""
        lowered = {k.lower(): v for k, v in record.items()}
        for k in keys:
            v = (record.get(k) or lowered.get(k.lower()) or "").strip()
            if v:
                return v
        return None

    #: Node fields that may carry the management IP, across NNMi versions.
    #: ``activeAddr`` is the node's active management address in current NNMi;
    #: the rest are older/alternate names kept as fallbacks.
    _IP_FIELDS = (
        "activeAddr",
        "managementAddress",
        "snmpAddress",
        "managementIpAddress",
        "primaryIpAddress",
        "ipAddress",
        "address",
    )

    _IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

    #: IPAddressBean fields: the address value, and the node it's hosted on.
    _IPADDR_VALUE_FIELDS = ("ipValue", "ipAddress", "address", "value")
    _IPADDR_NODE_FIELDS = ("hostedOnId", "hostedOnNodeId", "nodeId", "hostedOn")

    def _node_ip(self, record: dict) -> str | None:
        """Resolve a node's IP: a dedicated address field, else an IP-only name."""
        ip = self._first(record, self._IP_FIELDS)
        if ip:
            return ip
        # Some nodes are identified only by their address (no DNS name), so the
        # name/longName is itself the IP.
        for key in ("longName", "name"):
            val = (record.get(key) or "").strip()
            if self._IPV4_RE.match(val):
                return val
        return None

    def _fetch_ip_by_node(self, wanted: set[str]) -> dict[str, str]:
        """Map ``node id -> a management IP`` from IPAddressBean (best-effort).

        Fills in nodes that carry no address on the node record itself (e.g.
        instances that only return a DNS name). Only addresses for the ``wanted``
        node ids are kept, and paging stops as soon as every wanted node has one
        — so a huge IP table isn't read in full. One non-loopback IPv4 per node.
        Any failure returns whatever was found so far and never breaks host
        collection.
        """
        by_node: dict[str, str] = {}
        if not wanted:
            return by_node
        try:
            # IPAddress has no "name" attribute, so the usual name-LIKE-% filter
            # 500s; query by id instead. Catch EVERYTHING — optional enrichment.
            pages = self._iter_pages(
                "/IPAddressBeanService/IPAddressBean",
                _IPADDR_NS,
                "getIPAddresses",
                ("id", "GE", "0"),
                max_pages=_IP_MAX_PAGES,
            )
            for page in pages:
                for r in page:
                    node_id = self._first(r, self._IPADDR_NODE_FIELDS)
                    if not node_id or node_id not in wanted or node_id in by_node:
                        continue
                    ip = self._first(r, self._IPADDR_VALUE_FIELDS)
                    if not ip or ip.startswith("127.") or ip == "0.0.0.0" \
                            or not self._IPV4_RE.match(ip):
                        continue
                    by_node[node_id] = ip
                if wanted <= by_node.keys():  # every wanted node found -> stop
                    break
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment
            self.logger.warning("IPAddress fetch failed (partial, skipped): %s", exc)
        return by_node

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_nnmi_hosts(self.instance)

        records = self._fetch_with_fallback(
            "/NodeBeanService/NodeBean", _NODE_NS, "getNodes"
        )
        hosts: list[dict] = []
        # node id -> host dict, for nodes with no address on the node record.
        missing: dict[str, dict] = {}
        for r in records:
            status_str = (r.get("status") or "UNKNOWN").upper()
            mgmt = (r.get("managementMode") or "").upper()
            if mgmt in ("NOTMANAGED", "OUTOFSERVICE", "UNMANAGED"):
                status = HostStatus.disabled
            else:
                status = _NNMI_STATUS.get(status_str, HostStatus.unknown)
            host = {
                "external_id": r.get("id") or r.get("uuid") or r.get("name"),
                "hostname": r.get("name") or r.get("longName") or r.get("id"),
                "ip": self._node_ip(r),
                "status": status,
                "group_name": r.get("deviceCategory")
                or r.get("deviceFamily")
                or r.get("systemLocation"),
                "last_seen": datetime.now(timezone.utc),
                "raw_payload": r,
            }
            hosts.append(host)
            if not host["ip"] and r.get("id"):
                missing[str(r["id"])] = host

        # Only hit IPAddressBean when some nodes lacked a direct address (so
        # instances whose nodes all carry activeAddr pay no extra call).
        if missing:
            ip_by_node = self._fetch_ip_by_node(set(missing))
            for node_id, host in missing.items():
                ip = ip_by_node.get(node_id)
                if ip:
                    host["ip"] = ip
        return hosts

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_nnmi_alerts(self.instance)

        # Best-effort: never let an incident-fetch problem drop the node data.
        try:
            records = self._fetch(
                "/IncidentBeanService/IncidentBean",
                _INCIDENT_NS,
                "getIncidents",
                ("lifecycleState", "NE", "CLOSED"),
            )
        except CollectorError as exc:
            self.logger.warning("incident fetch failed: %s", exc)
            self.notes = f"Incidents unavailable: {exc}"
            return []

        alerts: list[dict] = []
        for r in records:
            sev = normalize_nnmi_severity(r.get("severity"))
            if sev < 2:  # skip NORMAL/INFO noise
                continue
            started = self._parse_epoch_millis(
                r.get("firstOccurrenceTime") or r.get("originOccurrenceTime")
            )
            alerts.append(
                {
                    "external_id": r.get("id") or r.get("uuid"),
                    "host_hostname": r.get("sourceNodeLongName")
                    or r.get("sourceNodeName")
                    or r.get("sourceObjectName"),
                    "severity_int": sev,
                    "severity_label": nnmi_severity_label(r.get("severity")),
                    "title": r.get("name") or r.get("message") or "NNMi incident",
                    "started_at": started,
                    "raw_payload": r,
                }
            )
        return alerts

    @staticmethod
    def _parse_epoch_millis(value: str | None) -> datetime | None:
        """Parse an NNMi epoch-millis timestamp string into a datetime."""
        if not value:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            return None
