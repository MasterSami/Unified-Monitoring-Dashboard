"""Self-contained demo incidents for the Incidents tab — ENABLE_MOCK_MODE_INCIDENT.

Independent of ``MOCK_MODE``, which drives the collectors and therefore every
OTHER tab (Agents, Alerts, Capacity, Topology): that flag is untouched by
this module. Nothing here reads or writes the database — every shape below
is hand-built to match the exact schemas ``/api/v1/incidents*`` already
returns for real Correlation/Incident rows, so the Incidents tab renders
identically whether it's reading this module or the real tables, while every
other tab keeps showing whatever ``MOCK_MODE`` says. See
:attr:`app.config.Settings.enable_mock_mode_incident`.

The five incidents below are fixed and re-timestamped relative to "now" on
every call, so a walkthrough always sees a fresh-looking, internally
consistent set — same story every time, never stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas import (
    AffectedEntityOut,
    IncidentAffectedOut,
    IncidentCorrelationGraphOut,
    IncidentDetailOut,
    IncidentEvidenceEntryOut,
    IncidentGraphEdgeOut,
    IncidentGraphNodeOut,
    IncidentHistoryOut,
    IncidentImpactOut,
    IncidentOut,
    IncidentTimelineEntryOut,
    RootCauseCandidateOut,
)

#: (entity_id, canonical_name, entity_type) — one fixed demo estate so every
#: incident below cross-references names an operator would recognise.
_DB01 = (8001, "db-prod-01", "database")
_GATEWAY = (8002, "api-gateway", "service")
_WEB = (8003, "web-frontend", "service")
_PAYMENT = (8004, "payment-api", "api")
_SWITCH = (8005, "core-switch-03", "network_device")
_APP12 = (8006, "app-server-12", "host")
_APP13 = (8007, "app-server-13", "host")
_AUTH = (8008, "auth-service", "service")
_LOGIN = (8009, "login-api", "api")
_BACKUP = (8010, "backup-host-05", "host")
_CHECKOUT = (8011, "checkout-api", "api")
_INVENTORY = (8012, "inventory-service", "service")
_REDIS = (8013, "cache-redis-01", "host")

_BUCKET_BY_TYPE = {
    "application": "applications", "service": "services", "api": "apis",
    "database": "databases", "host": "hosts", "network_device": "network_devices",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(minutes: int) -> datetime:
    return _now() - timedelta(minutes=minutes)


def _affected_from_members(members: list[tuple], *extra: tuple) -> dict[str, list[tuple]]:
    """Bucket a flat member list by entity_type, the same shape Incident.affected stores."""
    buckets: dict[str, list[tuple]] = {b: [] for b in _BUCKET_BY_TYPE.values()}
    for eid, name, etype in list(members) + list(extra):
        buckets[_BUCKET_BY_TYPE[etype]].append((eid, name, etype))
    return buckets


def _demo_incidents_raw() -> list[dict]:
    return [
        {
            "id": 9001,
            "title": "db-prod-01 is not responding — checkout and payments are affected",
            "status": "open", "severity_int": 5, "severity_label": "critical",
            "start_minutes_ago": 47, "update_minutes_ago": 2,
            "business_service": "E-commerce Platform",
            "correlation_types": ["known_dependency", "same_trace"],
            "sources": ["zabbix", "dynatrace"],
            "members": [_DB01, _GATEWAY, _WEB, _PAYMENT],
            "root_cause": [_DB01],
            "timeline": [
                (47, _DB01, "Connection pool exhausted on db-prod-01", "onset", "zabbix"),
                (45, _GATEWAY, "api-gateway: elevated 5xx rate on downstream calls", "onset", "dynatrace"),
                (44, _WEB, "web-frontend: checkout page timing out", "onset", "dynatrace"),
                (43, _PAYMENT, "payment-api: transaction confirmation delayed", "onset", "dynatrace"),
            ],
            "evidence": [
                ("known_dependency", "api-gateway depends on db-prod-01 (topology)", "topology_sync", _GATEWAY),
                ("same_trace", "trace 7f2a-91c links web-frontend to db-prod-01", "distributed_trace", _WEB),
                ("known_dependency", "payment-api depends on api-gateway (topology)", "topology_sync", _PAYMENT),
            ],
        },
        {
            "id": 9002,
            "title": "core-switch-03 is flapping — two app servers losing connectivity",
            "status": "open", "severity_int": 3, "severity_label": "warning",
            "start_minutes_ago": 140, "update_minutes_ago": 9,
            "business_service": "Core Network",
            "correlation_types": ["known_dependency"],
            "sources": ["nnmi", "zabbix"],
            "members": [_SWITCH, _APP12, _APP13],
            "root_cause": [_SWITCH],
            "timeline": [
                (140, _SWITCH, "core-switch-03: interface flapping on port Gi1/0/24", "onset", "nnmi"),
                (138, _APP12, "app-server-12: intermittent packet loss", "onset", "zabbix"),
                (135, _APP13, "app-server-13: intermittent packet loss", "onset", "zabbix"),
            ],
            "evidence": [
                ("known_dependency", "app-server-12 connects_to core-switch-03 (topology)", "topology_sync", _APP12),
                ("known_dependency", "app-server-13 connects_to core-switch-03 (topology)", "topology_sync", _APP13),
            ],
        },
        {
            "id": 9003,
            "title": "auth-service memory leak causing login timeouts",
            "status": "resolved", "severity_int": 4, "severity_label": "major",
            "start_minutes_ago": 1380, "update_minutes_ago": 610,
            "business_service": "Identity & Access",
            "correlation_types": ["same_entity"],
            "sources": ["dynatrace"],
            "members": [_AUTH, _LOGIN],
            "root_cause": [_AUTH],
            "timeline": [
                (1380, _AUTH, "auth-service: heap usage climbing steadily", "onset", "dynatrace"),
                (700, _LOGIN, "login-api: p95 latency above 4s", "onset", "dynatrace"),
                (610, _AUTH, "auth-service: restarted, heap usage back to baseline", "recovery", "dynatrace"),
            ],
            "evidence": [
                ("same_entity", "auth-service: 2 active problems on the same entity", "dedup", _AUTH),
            ],
        },
        {
            "id": 9004,
            "title": "backup-host-05 — CPU, memory and disk all elevated at once",
            "status": "open", "severity_int": 2, "severity_label": "minor",
            "start_minutes_ago": 38, "update_minutes_ago": 4,
            "business_service": "Backup & Archival",
            "correlation_types": ["same_entity"],
            "sources": ["zabbix"],
            "members": [_BACKUP],
            "root_cause": [_BACKUP],
            "timeline": [
                (38, _BACKUP, "backup-host-05: CPU above 90% during nightly job", "onset", "zabbix"),
                (36, _BACKUP, "backup-host-05: disk usage crossed 88%", "onset", "zabbix"),
            ],
            "evidence": [
                ("same_entity", "backup-host-05: 2 active problems on the same entity", "dedup", _BACKUP),
            ],
        },
        {
            "id": 9005,
            "title": "Checkout flow degraded — a single failing trace touches three services",
            "status": "acknowledged", "severity_int": 5, "severity_label": "critical",
            "start_minutes_ago": 22, "update_minutes_ago": 1,
            "business_service": "E-commerce Platform",
            "correlation_types": ["same_trace"],
            "sources": ["dynatrace"],
            "members": [_CHECKOUT, _INVENTORY, _REDIS],
            "root_cause": [_INVENTORY],
            "timeline": [
                (22, _INVENTORY, "inventory-service: stock lookup failing", "onset", "dynatrace"),
                (21, _REDIS, "cache-redis-01: cache miss rate spiking", "onset", "dynatrace"),
                (20, _CHECKOUT, "checkout-api: add-to-cart failing for ~14% of requests", "onset", "dynatrace"),
            ],
            "evidence": [
                ("same_trace", "trace 4b1e-83f links checkout-api, inventory-service, cache-redis-01", "distributed_trace", _INVENTORY),
            ],
        },
    ]


def _raw_by_id(incident_id: int) -> dict | None:
    return next((r for r in _demo_incidents_raw() if r["id"] == incident_id), None)


def _affected_out(raw: dict) -> IncidentAffectedOut:
    buckets = _affected_from_members(raw["members"])
    return IncidentAffectedOut(**{
        bucket: [AffectedEntityOut(entity_id=e[0], canonical_name=e[1]) for e in entities]
        for bucket, entities in buckets.items()
    })


def _root_cause_out(raw: dict) -> list[RootCauseCandidateOut]:
    out = []
    for eid, name, etype in raw["root_cause"]:
        evidence = [f"{name}: earliest problem onset in this incident"]
        out.append(RootCauseCandidateOut(
            entity_id=eid, entity_type=etype, canonical_name=name,
            evidence=evidence, evidence_count=len(evidence),
        ))
    return out


def _incident_out(raw: dict) -> IncidentOut:
    start = _ago(raw["start_minutes_ago"])
    return IncidentOut(
        id=raw["id"], title=raw["title"], status=raw["status"],
        severity_int=raw["severity_int"], severity_label=raw["severity_label"],
        start_time=start, last_update=_ago(raw["update_minutes_ago"]),
        business_service=raw["business_service"],
        related_events=[e[0] for e in raw["members"]],
        correlation_types=raw["correlation_types"],
        source_correlation_ids=[raw["id"]],
        affected=_affected_out(raw),
        root_cause_candidates=_root_cause_out(raw),
        member_roles={str(e[0]): ("root_cause" if e in raw["root_cause"] else "symptom") for e in raw["members"]},
        sources=raw["sources"],
        merged_into_id=None,
        created_at=start,
    )


def _timeline_out(raw: dict) -> list[IncidentTimelineEntryOut]:
    return [
        IncidentTimelineEntryOut(
            timestamp=_ago(mins), event_id=entity[0], entity_id=entity[0],
            entity_name=entity[1], description=desc, kind=kind, source_platform=platform,
        )
        for mins, entity, desc, kind, platform in raw["timeline"]
    ]


def _evidence_out(raw: dict) -> list[IncidentEvidenceEntryOut]:
    root_id = raw["root_cause"][0][0]
    return [
        IncidentEvidenceEntryOut(
            correlation_id=raw["id"], signal=signal, value=value, source=source,
            timestamp=_ago(raw["update_minutes_ago"]), related_event_id=entity[0],
            related_entity_id=entity[0], from_entity_id=entity[0], to_entity_id=root_id,
            rule_id=signal.upper(),
        )
        for signal, value, source, entity in raw["evidence"]
    ]


# --- Public read paths, mirroring the real /api/v1/incidents* handlers ------


def list_demo_incidents(
    *, status: str | None = None, severity_min: int | None = None,
) -> list[IncidentOut]:
    """The demo equivalent of ``list_incidents`` — status/severity filters
    only; the real endpoint's richer filters (member/source/affected-entity
    substring matches) aren't meaningful against five fixed rows.
    """
    rows = [_incident_out(r) for r in _demo_incidents_raw()]
    if status:
        rows = [r for r in rows if r.status == status]
    if severity_min is not None:
        rows = [r for r in rows if r.severity_int >= severity_min]
    return sorted(rows, key=lambda r: r.last_update, reverse=True)


def get_demo_incident(incident_id: int) -> IncidentDetailOut | None:
    raw = _raw_by_id(incident_id)
    if raw is None:
        return None
    return IncidentDetailOut(
        **_incident_out(raw).model_dump(),
        timeline=_timeline_out(raw),
        evidence=_evidence_out(raw),
    )


def get_demo_timeline(incident_id: int) -> list[IncidentTimelineEntryOut] | None:
    raw = _raw_by_id(incident_id)
    return None if raw is None else _timeline_out(raw)


def get_demo_evidence(incident_id: int) -> list[IncidentEvidenceEntryOut] | None:
    raw = _raw_by_id(incident_id)
    return None if raw is None else _evidence_out(raw)


def get_demo_impact(incident_id: int) -> IncidentImpactOut | None:
    raw = _raw_by_id(incident_id)
    if raw is None:
        return None
    return IncidentImpactOut(
        incident_id=raw["id"], affected=_affected_out(raw),
        business_service=raw["business_service"], application_health=[],
    )


def get_demo_correlation_graph(incident_id: int) -> IncidentCorrelationGraphOut | None:
    raw = _raw_by_id(incident_id)
    if raw is None:
        return None
    root_ids = {e[0] for e in raw["root_cause"]}
    nodes = [
        IncidentGraphNodeOut(
            entity_id=eid, entity_type=etype, canonical_name=name,
            role="root_cause_candidate" if eid in root_ids else "symptom",
            logical_event_ids=[eid],
        )
        for eid, name, etype in raw["members"]
    ]
    # A simple chain from the root cause outward — enough to draw a graph
    # that visually tells the same story as the evidence list.
    root = raw["root_cause"][0]
    edges = [
        IncidentGraphEdgeOut(from_entity_id=root[0], to_entity_id=member[0], relationship_type="calls", source="dynatrace")
        for member in raw["members"] if member != root
    ]
    return IncidentCorrelationGraphOut(incident_id=raw["id"], nodes=nodes, edges=edges)


def get_demo_history(incident_id: int) -> IncidentHistoryOut | None:
    raw = _raw_by_id(incident_id)
    if raw is None:
        return None
    incident_out = _incident_out(raw)
    affected = _affected_from_members(raw["members"])
    return IncidentHistoryOut(
        incident=incident_out.model_dump(mode="json"),
        events=[{"entity_id": e[0], "canonical_name": e[1]} for e in raw["members"]],
        entities=[{"entity_id": e[0], "canonical_name": e[1], "entity_type": e[2]} for e in raw["members"]],
        topology=[],
        correlation_signals=[
            {"signal": s, "value": v, "source": src} for s, v, src, _ in raw["evidence"]
        ],
        rules_triggered=raw["correlation_types"],
        root_cause_candidates=[c.model_dump() for c in _root_cause_out(raw)],
        operator_confirmation=[],
        resolution=None,
        resolution_time=None,
        business_impact={"business_service": raw["business_service"]},
        affected_services=[{"entity_id": e[0], "canonical_name": e[1]} for e in affected["services"]],
        affected_apis=[{"entity_id": e[0], "canonical_name": e[1]} for e in affected["apis"]],
        affected_databases=[{"entity_id": e[0], "canonical_name": e[1]} for e in affected["databases"]],
        timeline=[e.model_dump(mode="json") for e in _timeline_out(raw)],
    )
