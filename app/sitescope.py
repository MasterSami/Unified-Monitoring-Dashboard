"""SiteScope OM-integration log: redaction, parsing, and normalization.

This is the **canonical, tested** implementation of the SiteScope pipeline. The
on-box PowerShell forwarder redacts credentials and ships redacted raw lines; the
UMD parses and normalizes them here. Redaction is applied again on ingest as a
safety net, so a credential can never be stored even if a raw line slipped
through.

The log is tab-delimited with 23 fields. Field order was confirmed from live
samples (see ``FIELD`` below). Key rules:

* **State wins over severity** — if the event's state is a "cleared" state
  (``back to default`` / ``good`` / ``ok`` / ``no data``), severity is ``OK``
  regardless of the severity field. Implemented once in :func:`resolve_severity`.
* **Missing metric** — events carrying ``Alert has no defined Metric`` are kept
  and flagged with ``metric_missing=True`` (never dropped).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # zoneinfo is stdlib on 3.9+
    from zoneinfo import ZoneInfo

    _CAIRO = ZoneInfo("Africa/Cairo")
except Exception:  # pragma: no cover - fallback if tzdata missing
    _CAIRO = timezone.utc


# --- Field map (0-indexed), confirmed from live samples ---------------------
class FIELD:
    TIMESTAMP = 0
    SEVERITY = 1
    SOURCE = 2
    MONITOR_PATH = 3
    TARGET_IP = 4
    STATUS_MSG = 5
    DETAIL = 6
    MONITOR_TYPE = 8
    KEY_WITH_SEV = 9
    KEY = 10
    APP_NAME = 11
    SERVER_FQDN = 13
    DRILLDOWN_URL = 15
    MONITOR_ID = 16


EXPECTED_FIELDS = 23
_TS_FORMAT = "%Y/%m/%d %H:%M:%S:%f"

#: States that mean "recovered" — these force severity to OK.
STATE_OK = {"back to default", "good", "ok", "no data"}

#: SiteScope's native severities -> (unified int, native label).
_SEVERITY_MAP = {
    "critical": (5, "Critical"),
    "major": (4, "Major"),
    "minor": (3, "Minor"),
    "warning": (2, "Warning"),
    "normal": (1, "OK"),
    "ok": (1, "OK"),
    "": (1, "OK"),
}

_METRIC_MISSING_TOKEN = "alert has no defined metric"


# --- Redaction (whitelist-safe) ---------------------------------------------
# Any query parameter whose key contains one of these tokens is redacted. The
# ``(?!<REDACTED>)`` lookahead makes redaction count-idempotent, so re-running it
# as a safety net on already-redacted text does not inflate the counter.
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&;]\s*[A-Za-z0-9_.\-]*(?:user|pass|pwd|token|account|login)"
    r"[A-Za-z0-9_.\-]*=)(?!<REDACTED>)([^&\s\t;]*)"
)
# Explicit sensitive keys even without a leading ? & ; delimiter.
_SENSITIVE_KEY = re.compile(
    r"(?i)\b(sisuser|sispass|password|account|login|token)=(?!<REDACTED>)([^&\s\t;]*)"
)
# URL basic-auth  scheme://user:pass@host
_BASIC_AUTH = re.compile(r"(?i)(://)[^/@\s:]+:[^/@\s]+@")

_REDACTED = "<REDACTED>"


def redact(text: str) -> tuple[str, int]:
    """Redact embedded credentials. Returns (redacted_text, redactions_fired)."""
    if not text:
        return text, 0
    total = 0
    text, n = _SENSITIVE_QUERY.subn(r"\1" + _REDACTED, text)
    total += n
    text, n = _SENSITIVE_KEY.subn(r"\1=" + _REDACTED, text)
    total += n
    text, n = _BASIC_AUTH.subn(r"\1" + _REDACTED + "@", text)
    total += n
    return text, total


# --- Severity: the state-wins-over-severity rule (single source of truth) ----
def resolve_severity(severity_raw: str | None, state: str | None) -> tuple[str, int, bool]:
    """Return (severity_label, severity_int, resolved).

    If ``state`` is a cleared state (:data:`STATE_OK`), the event is ``OK`` and
    ``resolved=True`` no matter what the raw severity says. Otherwise the raw
    SiteScope severity is mapped to its native label.
    """
    st = (state or "").strip().lower()
    if st in STATE_OK:
        return "OK", 1, True
    sv = (severity_raw or "").strip().lower()
    if sv in _SEVERITY_MAP:
        sev_int, label = _SEVERITY_MAP[sv]
    else:
        sev_int, label = 2, (severity_raw or "Warning").strip().title()
    return label, sev_int, sev_int <= 1


# --- Identity helpers -------------------------------------------------------
def event_id(timestamp: str, host: str, monitor_name: str, state: str) -> str:
    """Stable 16-char id: sha256(timestamp|host|monitor_name|state)."""
    raw = f"{timestamp}|{host}|{monitor_name}|{state}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dedup_key(host: str | None) -> str:
    """Hostname lowercased with the domain suffix stripped.

    Matches the Zabbix/Dynatrace host-dedup convention used elsewhere in UMD.
    """
    h = (host or "").strip().lower()
    return h.split(".", 1)[0] if h else ""


# --- Parsing ----------------------------------------------------------------
def _clean(value: str | None) -> str:
    """Trim whitespace and the trailing ' ,' artifact SiteScope appends."""
    if value is None:
        return ""
    return value.strip().strip(",").strip()


def _extract_state(status_msg: str, detail: str) -> str:
    """State lives inside the message text, not a dedicated column."""
    m = re.search(r"threshold\s*\(([^)]*)\)", status_msg, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"crossed\s*'([^']*)'", detail or "", re.I)
    if m:
        return m.group(1).strip()
    return ""


def _extract_metric(key: str) -> tuple[str, str]:
    """From ``IP:GUID:metric`` return (metric, guid)."""
    parts = key.split(":")
    if len(parts) >= 3:
        return ":".join(parts[2:]).strip(), parts[1].strip()
    return "", ""


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        naive = datetime.strptime(raw.strip(), _TS_FORMAT)
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=_CAIRO).astimezone(timezone.utc)


@dataclass
class NormalizedEvent:
    """A SiteScope event mapped onto the shared alert schema (+ context)."""

    external_id: str  # == event_id, drives idempotent upsert
    source_instance: str
    host_hostname: str
    severity_label: str
    severity_int: int
    title: str
    state: str
    resolved: bool
    metric_missing: bool
    monitor_name: str
    dedup_key: str
    started_at: datetime | None
    raw_payload: dict = field(default_factory=dict)


class ParseError(ValueError):
    """Raised when a line cannot be parsed into the expected shape."""


def parse_line(line: str, source_instance: str) -> NormalizedEvent:
    """Parse one redacted tab-delimited log line into a NormalizedEvent.

    Redaction is applied here again as a safety net; the returned payload never
    contains raw credentials.
    """
    safe, _ = redact(line.rstrip("\r\n"))
    fields = safe.split("\t")
    if len(fields) < FIELD.MONITOR_ID + 1:
        raise ParseError(
            f"expected >= {FIELD.MONITOR_ID + 1} fields, got {len(fields)}"
        )

    ts_raw = fields[FIELD.TIMESTAMP].strip()
    severity_raw = _clean(fields[FIELD.SEVERITY])
    monitor_path = _clean(fields[FIELD.MONITOR_PATH])
    target_ip = _clean(fields[FIELD.TARGET_IP])
    status_msg = fields[FIELD.STATUS_MSG].strip()
    detail = fields[FIELD.DETAIL].strip()
    monitor_type = _clean(fields[FIELD.MONITOR_TYPE])
    key = _clean(fields[FIELD.KEY])
    app_name = _clean(fields[FIELD.APP_NAME])
    server_fqdn = _clean(fields[FIELD.SERVER_FQDN])
    drilldown, _ = redact(fields[FIELD.DRILLDOWN_URL].strip())
    monitor_id = _clean(fields[FIELD.MONITOR_ID])

    state = _extract_state(status_msg, detail)
    metric, guid = _extract_metric(key)
    metric_missing = (
        not metric or metric.strip().lower() == _METRIC_MISSING_TOKEN
    )

    # Correlation host: the application/node name; fall back to the target IP.
    host = app_name or target_ip
    label, sev_int, resolved = resolve_severity(severity_raw, state)

    monitor_short = monitor_path.split(":")[-1].strip() if monitor_path else ""
    title = status_msg or f"{monitor_short or monitor_path} — {state or severity_raw}"

    eid = event_id(ts_raw, host, monitor_path, state)

    return NormalizedEvent(
        external_id=eid,
        source_instance=source_instance,
        host_hostname=host,
        severity_label=label,
        severity_int=sev_int,
        title=title[:512],
        state=state,
        resolved=resolved,
        metric_missing=metric_missing,
        monitor_name=monitor_path,
        dedup_key=dedup_key(host),
        started_at=_parse_timestamp(ts_raw),
        raw_payload={
            "severity_raw": severity_raw,
            "target_ip": target_ip,
            "server_fqdn": server_fqdn,
            "monitor_type": monitor_type,
            "monitor_id": monitor_id,
            "metric": metric,
            "sitescope_guid": guid,
            "drilldown_url": drilldown,
        },
    )


# --- Host derivation --------------------------------------------------------
# SiteScope monitors *targets*; a Host is one monitored application/node. Status
# is inferred from the monitors on it, so SiteScope shows up as a full platform
# (hosts + alerts) alongside Zabbix / Dynatrace.


@dataclass
class DerivedHost:
    """A monitored target inferred from a set of SiteScope events."""

    external_id: str
    hostname: str
    ip: str | None
    status: str  # up | down | unknown  (matches HostStatus values)
    group_name: str | None
    last_seen: datetime | None
    raw_payload: dict = field(default_factory=dict)


def _monitor_group(monitor_path: str) -> str | None:
    """The SiteScope group folder — the 3rd segment of the monitor path."""
    parts = monitor_path.split(":")
    return parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None


def derive_hosts(events: list[NormalizedEvent]) -> list[DerivedHost]:
    """Roll a batch of events up to one host per monitored target.

    Status: ``down`` if any active event is Major/Critical (>=4); ``unknown`` if
    the target only reports "no data"; otherwise ``up`` (monitored and reporting).
    """
    agg: dict[str, dict] = {}
    for ev in events:
        key = ev.dedup_key or ev.host_hostname
        if not key:
            continue
        h = agg.get(key)
        if h is None:
            h = {
                "external_id": key,
                "hostname": ev.host_hostname or key,
                "ip": None,
                "worst_active": 0,
                "active": False,
                "nodata": False,
                "last_seen": None,
                "group": _monitor_group(ev.monitor_name),
                "monitor_type": ev.raw_payload.get("monitor_type"),
                "server_fqdn": ev.raw_payload.get("server_fqdn"),
            }
            agg[key] = h
        ip = ev.raw_payload.get("target_ip")
        if ip and not h["ip"]:
            h["ip"] = ip
        if not ev.resolved:
            h["active"] = True
            h["worst_active"] = max(h["worst_active"], ev.severity_int)
        if (ev.state or "").strip().lower() == "no data":
            h["nodata"] = True
        if ev.started_at and (h["last_seen"] is None or ev.started_at > h["last_seen"]):
            h["last_seen"] = ev.started_at

    hosts: list[DerivedHost] = []
    for h in agg.values():
        if h["worst_active"] >= 4:
            status = "down"
        elif h["nodata"] and not h["active"]:
            status = "unknown"
        else:
            status = "up"
        hosts.append(
            DerivedHost(
                external_id=h["external_id"],
                hostname=h["hostname"],
                ip=h["ip"],
                status=status,
                group_name=h["group"],
                last_seen=h["last_seen"],
                raw_payload={
                    "monitor_type": h["monitor_type"],
                    "server_fqdn": h["server_fqdn"],
                    "source": "sitescope",
                },
            )
        )
    return hosts
