"""Topology collection: NNMi network topology and Dynatrace service map.

Builds a node/edge graph per instance and stores it in the ``topology_nodes`` /
``topology_edges`` tables. NNMi contributes a Layer-2 network topology (devices
+ L2 links); Dynatrace contributes a service dependency map (services + calls).
The extraction recipes follow the provided export scripts.

Runs only when ``ENABLE_TOPOLOGY`` is set; wholesale-replaces each instance's
graph on every run (topology is a snapshot, and it changes rarely).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.collectors import build_collector
from app.collectors.nnmi import NnmiCollector
from app.config import Settings
from app.db import SessionLocal
from app.models import SourcePlatform, TopologyEdge, TopologyNode
from app.servers import ServerConfig, load_servers

logger = logging.getLogger("topology")

_L2_NS = "http://l2connection.sdk.nms.ov.hp.com/"
_NODE_NS = "http://node.sdk.nms.ov.hp.com/"

# Dynatrace relationship keys that mean "A calls B".
_DT_CALL_KEYS = {"calls"}


# --- NNMi network topology --------------------------------------------------


def collect_nnmi_topology(config: ServerConfig, settings: Settings) -> tuple[list, list]:
    """Return (nodes, edges) for one NNMi instance's L2 topology."""
    if settings.mock_mode:
        return _mock_network(config.name)

    collector = NnmiCollector(config, settings)
    node_records = collector._fetch_with_fallback(
        "/NodeBeanService/NodeBean", _NODE_NS, "getNodes"
    )
    l2_records = collector._fetch(
        "/L2ConnectionBeanService/L2ConnectionBean",
        _L2_NS,
        "getL2Connections",
        ("name", "LIKE", "%"),
    )

    nodes: list[dict] = []
    name_to_id: dict[str, str] = {}
    for r in node_records:
        ext = r.get("id") or r.get("uuid") or r.get("name")
        if not ext:
            continue
        name = r.get("name") or r.get("longName") or ext
        name_to_id[name] = ext
        nodes.append(
            {
                "external_id": str(ext),
                "kind": "device",
                "name": name,
                "category": r.get("deviceCategory") or r.get("deviceFamily"),
                "status": (r.get("status") or "").upper() or None,
                "attributes": {
                    k: r[k]
                    for k in (
                        "managementAddress",
                        "deviceVendor",
                        "deviceModel",
                        "systemLocation",
                    )
                    if r.get(k)
                },
            }
        )

    edges: list[dict] = []
    for i, l2 in enumerate(l2_records):
        conn_name = l2.get("name") or ""
        endpoints = [ep.strip() for ep in conn_name.split(",") if ep.strip()]
        node_ids: list[str] = []
        for ep in endpoints:
            node_name = ep.split("[", 1)[0].strip()
            ext = name_to_id.get(node_name)
            if ext:
                node_ids.append(str(ext))
        base_id = l2.get("id") or f"l2-{i}"
        for a, b in zip(node_ids, node_ids[1:]):
            edges.append(
                {
                    "external_id": f"{base_id}:{a}:{b}",
                    "from_external_id": a,
                    "to_external_id": b,
                    "kind": "l2",
                    "label": conn_name[:200],
                    "attributes": {},
                }
            )
    return nodes, edges


# --- Dynatrace service map --------------------------------------------------


def collect_dynatrace_topology(config: ServerConfig, settings: Settings) -> tuple[list, list]:
    """Return (nodes, edges) for one Dynatrace instance's service map."""
    if settings.mock_mode:
        return _mock_service_map(config.name)

    collector = build_collector(config, settings)
    base = config.url.rstrip("/")
    headers = {
        "Authorization": f"Api-Token {config.token}",
        "Accept": "application/json",
    }
    url = f"{base}/api/v2/entities"
    params = {
        "entitySelector": 'type("SERVICE")',
        "fields": "+fromRelationships,+toRelationships,+properties",
        "pageSize": "500",
    }
    entities: list[dict] = []
    with collector._client(headers=headers) as client:  # type: ignore[union-attr]
        while url:
            resp = collector._request_with_retries(client, "GET", url, params=params)  # type: ignore[union-attr]
            resp.raise_for_status()
            data = resp.json()
            entities.extend(data.get("entities", []))
            next_key = data.get("nextPageKey")
            if next_key:
                params = {"nextPageKey": next_key}
            else:
                url = ""

    ids = set()
    nodes: list[dict] = []
    for e in entities:
        eid = e.get("entityId")
        if not eid:
            continue
        ids.add(eid)
        props = e.get("properties") or {}
        nodes.append(
            {
                "external_id": eid,
                "kind": "service",
                "name": e.get("displayName") or eid,
                "category": props.get("serviceType"),
                "status": None,
                "attributes": {"serviceType": props.get("serviceType")},
            }
        )

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in entities:
        eid = e.get("entityId")
        if not eid:
            continue
        for key, targets in (e.get("fromRelationships") or {}).items():
            if key not in _DT_CALL_KEYS:
                continue
            for t in targets or []:
                tid = t.get("id") if isinstance(t, dict) else t
                if not tid or tid == eid or (eid, tid) in seen:
                    continue
                seen.add((eid, tid))
                edges.append(
                    {
                        "external_id": f"{eid}->{tid}",
                        "from_external_id": eid,
                        "to_external_id": tid,
                        "kind": "call",
                        "label": key,
                        "attributes": {},
                    }
                )
    # Keep only edges whose endpoints are known services.
    edges = [
        e
        for e in edges
        if e["from_external_id"] in ids and e["to_external_id"] in ids
    ]
    return nodes, edges


# --- Persistence ------------------------------------------------------------


def _replace_graph(
    db: Session,
    platform: SourcePlatform,
    instance: str,
    nodes: list[dict],
    edges: list[dict],
) -> None:
    """Replace the stored graph for one instance (snapshot semantics)."""
    db.execute(
        delete(TopologyEdge).where(
            TopologyEdge.source_platform == platform,
            TopologyEdge.source_instance == instance,
        )
    )
    db.execute(
        delete(TopologyNode).where(
            TopologyNode.source_platform == platform,
            TopologyNode.source_instance == instance,
        )
    )
    for n in nodes:
        db.add(
            TopologyNode(
                source_platform=platform,
                source_instance=instance,
                external_id=str(n["external_id"]),
                kind=n.get("kind", "node"),
                name=n.get("name") or str(n["external_id"]),
                category=n.get("category"),
                status=n.get("status"),
                attributes=n.get("attributes", {}),
            )
        )
    # Only keep edges whose endpoints exist as nodes.
    node_ids = {str(n["external_id"]) for n in nodes}
    for e in edges:
        if (
            e["from_external_id"] not in node_ids
            or e["to_external_id"] not in node_ids
        ):
            continue
        db.add(
            TopologyEdge(
                source_platform=platform,
                source_instance=instance,
                external_id=str(e["external_id"]),
                from_external_id=str(e["from_external_id"]),
                to_external_id=str(e["to_external_id"]),
                kind=e.get("kind", "link"),
                label=e.get("label"),
                attributes=e.get("attributes", {}),
            )
        )
    db.commit()


def run_topology(settings: Settings) -> None:
    """Collect topology for every configured NNMi / Dynatrace instance."""
    db = SessionLocal()
    try:
        for cfg in load_servers(settings):
            try:
                if cfg.platform == "nnmi":
                    nodes, edges = collect_nnmi_topology(cfg, settings)
                    platform = SourcePlatform.nnmi
                elif cfg.platform == "dynatrace":
                    nodes, edges = collect_dynatrace_topology(cfg, settings)
                    platform = SourcePlatform.dynatrace
                else:
                    continue
                _replace_graph(db, platform, cfg.name, nodes, edges)
                logger.info(
                    "topology %s: %d nodes, %d edges", cfg.name, len(nodes), len(edges)
                )
            except Exception as exc:  # noqa: BLE001 — contained per instance
                db.rollback()
                logger.warning("topology collection failed for %s: %s", cfg.name, exc)
    finally:
        db.close()


def has_topology(settings: Settings) -> bool:
    """True if any topology rows exist (used to seed the first run)."""
    db = SessionLocal()
    try:
        return db.scalar(select(TopologyNode.id).limit(1)) is not None
    finally:
        db.close()


# --- Mock data --------------------------------------------------------------


def _mock_network(instance: str) -> tuple[list, list]:
    slug = instance.lower().replace(" ", "-")
    devices = [
        ("core-01", "Router", "NORMAL"),
        ("core-02", "Router", "NORMAL"),
        ("dist-01", "Switch", "NORMAL"),
        ("dist-02", "Switch", "MAJOR"),
        ("acc-01", "Switch", "NORMAL"),
        ("acc-02", "Switch", "NORMAL"),
        ("fw-01", "Firewall", "NORMAL"),
        ("wan-01", "Router", "NORMAL"),
    ]
    nodes = [
        {
            "external_id": f"{slug}-{name}",
            "kind": "device",
            "name": f"{name}.{slug}",
            "category": cat,
            "status": st,
            "attributes": {"managementAddress": f"192.168.{i}.1"},
        }
        for i, (name, cat, st) in enumerate(devices, start=1)
    ]
    links = [
        ("core-01", "core-02"),
        ("core-01", "dist-01"),
        ("core-02", "dist-02"),
        ("dist-01", "acc-01"),
        ("dist-01", "acc-02"),
        ("dist-02", "acc-02"),
        ("core-01", "fw-01"),
        ("fw-01", "wan-01"),
        ("core-02", "wan-01"),
    ]
    edges = [
        {
            "external_id": f"{slug}-l2-{i}",
            "from_external_id": f"{slug}-{a}",
            "to_external_id": f"{slug}-{b}",
            "kind": "l2",
            "label": f"{a} ↔ {b}",
            "attributes": {},
        }
        for i, (a, b) in enumerate(links)
    ]
    return nodes, edges


def _mock_service_map(instance: str) -> tuple[list, list]:
    slug = instance.lower().replace(" ", "-")
    services = [
        ("frontend", "WEB_REQUEST_SERVICE"),
        ("api-gateway", "WEB_SERVICE"),
        ("orders", "WEB_SERVICE"),
        ("payments", "WEB_SERVICE"),
        ("inventory", "WEB_SERVICE"),
        ("auth", "WEB_SERVICE"),
        ("orders-db", "DATABASE"),
        ("payments-mq", "MESSAGING"),
    ]
    nodes = [
        {
            "external_id": f"{slug}-svc-{name}",
            "kind": "middleware" if cat in ("DATABASE", "MESSAGING") else "service",
            "name": name,
            "category": cat,
            "status": None,
            "attributes": {"serviceType": cat},
        }
        for name, cat in services
    ]
    calls = [
        ("frontend", "api-gateway"),
        ("api-gateway", "orders"),
        ("api-gateway", "auth"),
        ("orders", "payments"),
        ("orders", "inventory"),
        ("orders", "orders-db"),
        ("payments", "payments-mq"),
        ("payments", "auth"),
    ]
    edges = [
        {
            "external_id": f"{slug}-call-{i}",
            "from_external_id": f"{slug}-svc-{a}",
            "to_external_id": f"{slug}-svc-{b}",
            "kind": "call",
            "label": "calls",
            "attributes": {},
        }
        for i, (a, b) in enumerate(calls)
    ]
    return nodes, edges
