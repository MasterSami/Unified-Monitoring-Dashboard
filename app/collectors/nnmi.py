"""NNMi collector using the SOAP web services.

Nodes come from the NodeBean service, open incidents from the IncidentBean
service. We use raw SOAP envelopes over httpx + lxml rather than zeep: NNMi's
WSDLs are large and often fight zeep's schema parsing, and hand-built envelopes
keep the dependency surface small and the behavior predictable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from app.collectors.base import BaseCollector, CollectorError
from app.collectors import mock_data
from app.config import Settings
from app.models import HostStatus, SourcePlatform
from app.normalizer import normalize_nnmi_severity
from app.servers import ServerConfig

# NNMi status string -> normalized host status.
_NNMI_STATUS = {
    "NORMAL": HostStatus.up,
    "WARNING": HostStatus.up,
    "MINOR": HostStatus.up,
    "MAJOR": HostStatus.down,
    "CRITICAL": HostStatus.down,
    "UNKNOWN": HostStatus.unknown,
    "NO_STATUS": HostStatus.unknown,
}

_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"


class NnmiCollector(BaseCollector):
    """Collects nodes and open incidents from NNMi via SOAP."""

    name = "nnmi"
    platform = SourcePlatform.nnmi

    def __init__(self, config: ServerConfig, settings: Settings) -> None:
        super().__init__(config, settings)
        base = config.url.rstrip("/")
        self._node_url = f"{base}/NodeBeanService/NodeBean"
        self._incident_url = f"{base}/IncidentBeanService/IncidentBean"

    # --- SOAP helper --------------------------------------------------------

    def _soap_call(self, url: str, body_xml: str, soap_action: str) -> etree._Element:
        """POST a SOAP envelope and return the parsed response body element."""
        envelope = (
            f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}">'
            "<soapenv:Header/>"
            f"<soapenv:Body>{body_xml}</soapenv:Body>"
            "</soapenv:Envelope>"
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": soap_action,
        }
        auth = (self.config.user, self.config.password)
        with self._client(headers=headers, auth=auth) as client:
            resp = self._request_with_retries(
                client, "POST", url, content=envelope.encode("utf-8")
            )
            resp.raise_for_status()
        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:  # pragma: no cover - defensive
            raise CollectorError(f"invalid SOAP response: {exc}") from exc
        body = root.find(f"{{{_SOAP_ENV}}}Body")
        if body is None:
            raise CollectorError("SOAP response missing Body")
        return body

    @staticmethod
    def _text(el: etree._Element, tag: str) -> str | None:
        """Return the text of the first descendant whose local-name matches."""
        found = el.find(f".//*[local-name()='{tag}']")
        return found.text if found is not None else None

    # --- Contract -----------------------------------------------------------

    def collect_hosts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_nnmi_hosts(self.instance)

        body = self._soap_call(
            self._node_url,
            '<getNodes xmlns="http://nnm.hp.com/2008/04/NodeBean"/>',
            "getNodes",
        )
        hosts: list[dict] = []
        for item in body.findall(".//*[local-name()='item']"):
            status_str = (self._text(item, "status") or "UNKNOWN").upper()
            hosts.append(
                {
                    "external_id": self._text(item, "id")
                    or self._text(item, "uuid")
                    or self._text(item, "name"),
                    "hostname": self._text(item, "name"),
                    "ip": self._text(item, "managementAddress"),
                    "status": _NNMI_STATUS.get(status_str, HostStatus.unknown),
                    "group_name": self._text(item, "systemLocation"),
                    "last_seen": datetime.now(timezone.utc),
                    "raw_payload": {"status": status_str},
                }
            )
        return hosts

    def collect_alerts(self) -> list[dict]:
        if self.settings.mock_mode:
            return mock_data.mock_nnmi_alerts(self.instance)

        body = self._soap_call(
            self._incident_url,
            '<getIncidents xmlns="http://nnm.hp.com/2008/04/IncidentBean">'
            "<filter><condition><name>lifecycleState</name>"
            "<operator>EQ</operator><value>REGISTERED</value></condition></filter>"
            "</getIncidents>",
            "getIncidents",
        )
        alerts: list[dict] = []
        for item in body.findall(".//*[local-name()='item']"):
            started = None
            ts = self._text(item, "firstOccurrenceTime") or self._text(
                item, "createTime"
            )
            if ts:
                try:
                    started = datetime.fromtimestamp(
                        int(ts) / 1000, tz=timezone.utc
                    )
                except (ValueError, TypeError):
                    started = None
            alerts.append(
                {
                    "external_id": self._text(item, "id")
                    or self._text(item, "uuid"),
                    "host_hostname": self._text(item, "sourceNodeName")
                    or self._text(item, "sourceName"),
                    "severity_int": normalize_nnmi_severity(
                        self._text(item, "severity")
                    ),
                    "title": self._text(item, "name")
                    or self._text(item, "message")
                    or "",
                    "started_at": started,
                    "raw_payload": {"severity": self._text(item, "severity")},
                }
            )
        return alerts
