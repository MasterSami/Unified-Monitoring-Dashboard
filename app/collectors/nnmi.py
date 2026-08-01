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

_PAGE_SIZE = 500
_MAX_PAGES = 40  # safety cap (up to 20k objects/entity)
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

    def _fetch(
        self,
        path: str,
        ns: str,
        operation: str,
        cond: tuple[str, str, str],
    ) -> list[dict]:
        """Fetch all pages for one entity/condition."""
        rows: list[dict] = []
        offset = 0
        for _ in range(_MAX_PAGES):
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
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
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

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_nnmi_hosts(self.instance)

        records = self._fetch_with_fallback(
            "/NodeBeanService/NodeBean", _NODE_NS, "getNodes"
        )
        hosts: list[dict] = []
        for r in records:
            status_str = (r.get("status") or "UNKNOWN").upper()
            mgmt = (r.get("managementMode") or "").upper()
            if mgmt in ("NOTMANAGED", "OUTOFSERVICE", "UNMANAGED"):
                status = HostStatus.disabled
            else:
                status = _NNMI_STATUS.get(status_str, HostStatus.unknown)
            hosts.append(
                {
                    "external_id": r.get("id") or r.get("uuid") or r.get("name"),
                    "hostname": r.get("name") or r.get("longName") or r.get("id"),
                    "ip": r.get("managementAddress") or None,
                    "status": status,
                    "group_name": r.get("deviceCategory")
                    or r.get("deviceFamily")
                    or r.get("systemLocation"),
                    "last_seen": datetime.now(timezone.utc),
                    "raw_payload": r,
                }
            )
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
