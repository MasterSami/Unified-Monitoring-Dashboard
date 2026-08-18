"""Topology collection: NNMi L2 network + Dynatrace unified dependency map.

Two graphs are built per instance and stored in ``topology_nodes`` /
``topology_edges``:

* **NNMi** — a Layer-2 network topology (devices + L2 connections). Each L2
  connection keeps its status, endpoints and interface ids, matching the
  ``NNMI_l2_connections.csv`` export.
* **Dynatrace** — a *unified* service dependency map: services are grouped into
  business applications, middleware (MQ / ESB / DB) is classified and made
  transparent, and each row is a resolved path
  ``Source App / Service → [middleware chain] → Target Service / App`` — matching
  the ``Unified_Map`` / ``App_to_App`` / ``Service_to_Service`` sheets. The
  extraction is a port of the provided ``dynatrace_unified_map.py``.

Runs only when ``ENABLE_TOPOLOGY`` is set; each instance's graph is replaced
wholesale on every run (topology changes rarely).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

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


# ===========================================================================
# NNMi network topology (L2)
# ===========================================================================


def collect_nnmi_topology(config: ServerConfig, settings: Settings) -> tuple[list, list]:
    """Return (nodes, edges) for one NNMi instance's L2 topology.

    Every L2 connection is kept (endpoint devices missing from NodeBean are
    synthesized as nodes), so the connections table mirrors the NNMi export.
    """
    if settings.mock_mode:
        return _mock_network(config.name)

    collector = NnmiCollector(config, settings)
    node_records = collector._fetch_with_fallback(
        "/NodeBeanService/NodeBean", _NODE_NS, "getNodes"
    )
    # Some NNMi versions return an empty page for ``name LIKE %`` on the
    # L2ConnectionBean endpoint.  The standalone export uses ``id GE 0`` as a
    # fallback, so use the collector's equivalent helper here as well.  Without
    # it the Nodes page can populate while the L2 table remains empty.
    l2_records = collector._fetch_with_fallback(
        "/L2ConnectionBeanService/L2ConnectionBean", _L2_NS, "getL2Connections"
    )

    nodes: list[dict] = []
    name_to_id: dict[str, str] = {}
    for r in node_records:
        ext = r.get("id") or r.get("uuid") or r.get("name")
        if not ext:
            continue
        name = r.get("name") or r.get("longName") or ext
        # L2 endpoint names are strings embedded in ``name`` and can differ
        # from NodeBean only by letter case.  Keep this lookup normalized to
        # avoid showing duplicate synthetic nodes in an otherwise valid graph.
        name_to_id[name.strip().casefold()] = str(ext)
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
    synth: dict[str, str] = {}

    def _node_id(device_name: str) -> str:
        """Resolve a device name to a node id, synthesizing one if unknown."""
        key = device_name.strip().casefold()
        if key in name_to_id:
            return name_to_id[key]
        if key not in synth:
            nid = f"syn:{device_name}"
            synth[key] = nid
            nodes.append(
                {
                    "external_id": nid,
                    "kind": "device",
                    "name": device_name,
                    "category": None,
                    "status": None,
                    "attributes": {"synthesized": True},
                }
            )
        return synth[key]

    for i, l2 in enumerate(l2_records):
        # ``name`` normally contains ``NodeA[ifA],NodeB[ifB]``.  Keep an ID
        # fallback as a visible table value for malformed NNMi records.
        conn_name = l2.get("name") or str(l2.get("id") or f"L2 connection {i + 1}")
        endpoints = [ep.strip() for ep in conn_name.split(",") if ep.strip()]
        # "Device[Interface]" -> device name is the part before "[".
        device_names = [ep.split("[", 1)[0].strip() for ep in endpoints]
        node_ids = [_node_id(dn) for dn in device_names if dn]
        base_id = l2.get("id") or f"l2-{i}"
        status = (l2.get("status") or "").upper() or None
        attrs = {
            "name": conn_name,
            "status": status,
            "interfaces": l2.get("interfaces") or "",
        }
        for j, ep in enumerate(endpoints[:4], start=1):
            attrs[f"endpoint{j}"] = ep
        # Do not lose an L2 record merely because NNMi returned an incomplete
        # endpoint label.  A placeholder keeps it visible in the L2 table and
        # makes the data problem obvious in the graph instead of silently
        # dropping the connection.
        while len(node_ids) < 2:
            node_ids.append(_node_id(f"Unknown endpoint ({base_id}) #{len(node_ids) + 1}"))
        # Chain consecutive endpoints so multi-access segments still connect.
        for a, b in zip(node_ids, node_ids[1:]):
            edges.append(
                {
                    "external_id": f"{base_id}:{a}:{b}",
                    "from_external_id": a,
                    "to_external_id": b,
                    "kind": "l2",
                    "label": conn_name[:250],
                    "status": status,
                    "attributes": attrs,
                }
            )
    return nodes, edges


# ===========================================================================
# Dynatrace unified dependency map (port of dynatrace_unified_map.py)
# ===========================================================================

_DT_FIELDS = "+fromRelationships,+toRelationships,+properties,+managementZones,+tags"
_DT_ENTITY_TYPES = ("SERVICE", "QUEUE", "APPLICATION")
_DT_LOOKBACK = "now-30d"
_MAX_MW_HOPS = 5
UNMAPPED = "UNMAPPED"

# Relationship keys that mean "A calls / feeds B" (and the reverse).
_CALL_REL_FROM = {"calls", "produces", "sends", "writesTo", "producesQueue"}
_CALL_REL_TO = {"calledBy", "consumes", "consumedBy", "producedBy", "receives"}

# Middleware classification.
_MW_ENTITY_TYPES = {"QUEUE", "QUEUE_INSTANCE"}
_MW_SERVICE_TYPES = {"MESSAGING", "QUEUE", "DATABASE", "ESB", "MESSAGE_BROKER"}
_MW_CATEGORY = {
    "Messaging": [
        "MQ", "IBM_MQ", "WEBSPHERE_MQ", "KAFKA", "RABBIT", "ACTIVEMQ", "ACTIVE_MQ",
        "JMS", "TIBCO", "AMQP", "MSMQ", "SQS", "PUBSUB", "SERVICE_BUS", "NATS",
    ],
    "ESB / Integration": ["ESB", "MULE", "CAMEL", "BIZTALK", "WSO2", "BOOMI", "OSB"],
    "Gateway / Proxy": [
        "APIGEE", "KONG", "ENVOY", "ISTIO", "HAPROXY", "NGINX", "F5",
        "API_GATEWAY", "APIGATEWAY", "TRAFFIC_MANAGER",
    ],
    "Database": [
        "ORACLE", "SQL_SERVER", "MSSQL", "MYSQL", "POSTGRES", "MONGO", "REDIS",
        "CASSANDRA", "DB2", "HANA", "ELASTICSEARCH",
    ],
}

# Application grouping (priority: tag -> name pattern -> management zone -> RUM).
_APP_TAG_KEYS = ["Application", "App", "System", "BusinessApp", "Service_Owner"]
_APP_NAME_PATTERNS = [
    (r"\b(billing|invoic|charg|rating|mediation)", "Billing"),
    (r"\b(crm|customer|subscriber|account)", "CRM"),
    (r"\b(portal|selfcare|self-care|frontend)", "Portal"),
    (r"\b(payment|wallet|topup|top-up|recharge)", "Payments"),
    (r"\b(provision|activation|order|oms|fulfil)", "Order Management"),
    (r"\b(auth|identity|sso|iam|login)", "Identity"),
    (r"\b(notif|sms|email|push|campaign)", "Notifications"),
    (r"\b(report|analytic|dwh|datawarehouse)|\bbi\b", "Reporting"),
]
_MZ_IGNORE_PATTERNS = [
    r"^all\b", r"\binfra", r"\bhost", r"\bdefault\b",
    r"\bnetwork\b", r"\bmonitoring\b", r"\bdatabase[s]?\b",
]
_MZ_STRIP_PATTERNS = [
    r"^(app|application|prod|production|uat|dev|test|sit)[\s\-_:]+",
    r"[\s\-_:]+(prod|production|uat|dev|test|sit|dr)$",
]


def _dt_props(e: dict) -> dict:
    return e.get("properties") or {}


def _dt_tech(e: dict) -> str:
    p, bits = _dt_props(e), []
    for t in p.get("softwareTechnologies") or []:
        bits.append(str(t.get("type", "")) if isinstance(t, dict) else str(t))
    for k in ("serviceTechnologyTypes", "technologies", "databaseVendor",
              "databaseName", "webServerName"):
        v = p.get(k)
        if isinstance(v, list):
            bits.extend(str(x) for x in v)
        elif v:
            bits.append(str(v))
    return ", ".join(sorted({b for b in bits if b}))


def _classify_middleware(node: dict) -> tuple[bool, str]:
    """Return (is_middleware, category)."""
    etype = node["entity_type"]
    stype = (node["service_type"] or "").upper()
    blob = f"{node['technology']} {node['name']}".upper().replace("-", "_").replace(" ", "_")
    if etype in _MW_ENTITY_TYPES:
        return True, "Messaging"
    if stype == "DATABASE":
        return True, "Database"
    if stype in _MW_SERVICE_TYPES:
        cat = "Messaging" if stype in {"MESSAGING", "QUEUE", "MESSAGE_BROKER"} else "ESB / Integration"
        return True, cat
    for cat, kws in _MW_CATEGORY.items():
        for kw in kws:
            if kw in blob:
                return True, cat
    return False, ""


def _dt_build_graph(entities: list[dict]):
    nodes: dict[str, dict] = {}
    out_edges: dict[str, set] = defaultdict(set)
    rum_calls: dict[str, set] = defaultdict(set)

    for e in entities:
        eid = e.get("entityId")
        if not eid:
            continue
        p = _dt_props(e)
        nodes[eid] = {
            "id": eid,
            "name": e.get("displayName") or eid,
            "entity_type": e.get("type") or eid.split("-")[0],
            "service_type": p.get("serviceType") or "",
            "technology": _dt_tech(e),
            "mzs": [m.get("name", "") for m in (e.get("managementZones") or [])],
            "tags": e.get("tags") or [],
        }

    for e in entities:
        eid = e.get("entityId")
        if not eid:
            continue
        is_rum = e.get("type") == "APPLICATION"
        for key, targets in (e.get("fromRelationships") or {}).items():
            if key not in _CALL_REL_FROM:
                continue
            ids = [(t.get("id") if isinstance(t, dict) else t) for t in (targets or [])]
            ids = [i for i in ids if i and i != eid]
            if is_rum:
                for i in ids:
                    rum_calls[i].add(eid)
                continue
            for i in ids:
                out_edges[eid].add(i)
        for key, sources in (e.get("toRelationships") or {}).items():
            if key not in _CALL_REL_TO:
                continue
            for s in sources or []:
                sid = s.get("id") if isinstance(s, dict) else s
                if not sid or sid == eid:
                    continue
                if sid.startswith("APPLICATION-"):
                    rum_calls[eid].add(sid)
                    continue
                out_edges[sid].add(eid)

    for rid in set(out_edges) | {t for v in out_edges.values() for t in v}:
        nodes.setdefault(rid, {
            "id": rid, "name": rid, "entity_type": rid.split("-")[0],
            "service_type": "", "technology": "", "mzs": [], "tags": [],
        })
    return nodes, out_edges, rum_calls


def _clean_mz(mz: str) -> str | None:
    low = mz.strip().lower()
    if not low:
        return None
    for pat in _MZ_IGNORE_PATTERNS:
        if re.search(pat, low, re.I):
            return None
    out = mz.strip()
    for pat in _MZ_STRIP_PATTERNS:
        out = re.sub(pat, "", out, flags=re.I).strip()
    return out or None


def _assign_app(node: dict, nodes: dict, rum_calls: dict) -> str:
    for t in node["tags"]:
        key, val = (t.get("key") or "").strip(), (t.get("value") or "").strip()
        for want in _APP_TAG_KEYS:
            if key.lower() == want.lower() and val:
                return val
    low = node["name"].lower()
    for pat, app in _APP_NAME_PATTERNS:
        if re.search(pat, low, re.I):
            return app
    for mz in node["mzs"]:
        c = _clean_mz(mz)
        if c:
            return c
    apps = rum_calls.get(node["id"])
    if apps:
        aid = sorted(apps)[0]
        return nodes.get(aid, {}).get("name", aid)
    return UNMAPPED


def _resolve_paths(nodes: dict, out_edges: dict, mw: dict) -> list[tuple]:
    """Walk the graph treating middleware nodes as transparent hops."""
    paths: list[tuple] = []
    business = [
        i for i, n in nodes.items()
        if not mw[i][0] and n["entity_type"] != "APPLICATION"
    ]
    for src in business:
        stack = [(nxt, [], {src}) for nxt in out_edges.get(src, ())]
        while stack:
            cur, chain, visited = stack.pop()
            if cur in visited:
                continue
            visited = visited | {cur}
            if mw.get(cur, (False,))[0]:
                if len(chain) >= _MAX_MW_HOPS:
                    continue
                for nxt in out_edges.get(cur, ()):
                    stack.append((nxt, chain + [cur], visited))
            else:
                paths.append((src, chain, cur))
    seen, uniq = set(), []
    for s, c, t in paths:
        k = (s, tuple(c), t)
        if k not in seen:
            seen.add(k)
            uniq.append((s, c, t))
    return uniq


def collect_dynatrace_topology(config: ServerConfig, settings: Settings) -> tuple[list, list]:
    """Return (nodes, edges) for one Dynatrace instance's unified map.

    Nodes are the business services that take part in a resolved path; each edge
    is one resolved path carrying app, middleware-chain and link-type detail.
    """
    if settings.mock_mode:
        return _mock_service_map(config.name)

    entities = _fetch_dynatrace_entities(config, settings)
    nodes_raw, out_edges, rum_calls = _dt_build_graph(entities)
    mw = {i: _classify_middleware(n) for i, n in nodes_raw.items()}
    app_of: dict[str, str] = {}
    for i, n in nodes_raw.items():
        if mw[i][0] or n["entity_type"] == "APPLICATION":
            app_of[i] = "<middleware>" if mw[i][0] else "<rum>"
        else:
            app_of[i] = _assign_app(n, nodes_raw, rum_calls)

    paths = _resolve_paths(nodes_raw, out_edges, mw)

    def nm(i: str) -> str:
        return nodes_raw[i]["name"]

    nodes: list[dict] = []
    seen_nodes: set[str] = set()

    def _ensure_node(sid: str) -> None:
        if sid in seen_nodes:
            return
        seen_nodes.add(sid)
        n = nodes_raw[sid]
        nodes.append({
            "external_id": sid,
            "kind": "service",
            "name": n["name"],
            "category": n["service_type"] or None,
            "status": None,
            "attributes": {
                "application": app_of.get(sid, UNMAPPED),
                "service_type": n["service_type"] or "",
                "technology": n["technology"] or "",
            },
        })

    edges: list[dict] = []
    for s, chain, t in paths:
        _ensure_node(s)
        _ensure_node(t)
        mw_names = " → ".join(nm(m) for m in chain)
        mw_types = ", ".join(sorted({mw[m][1] for m in chain if mw[m][1]}))
        sa, ta = app_of.get(s, UNMAPPED), app_of.get(t, UNMAPPED)
        full = (
            f"{sa} / {nm(s)}"
            + (f"  →[{mw_names}]→  " if chain else "  →  ")
            + f"{nm(t)} / {ta}"
        )
        chain_ids = ">".join(chain)
        edges.append({
            "external_id": f"{s}|{chain_ids}|{t}",
            "from_external_id": s,
            "to_external_id": t,
            "kind": "path",
            "label": "Via Middleware" if chain else "Direct",
            "attributes": {
                "source_application": sa,
                "source_service": nm(s),
                "source_service_type": nodes_raw[s]["service_type"] or "-",
                "link_type": "Via Middleware" if chain else "Direct",
                "middleware_chain": mw_names or "-",
                "middleware_type": mw_types or "-",
                "mw_hops": len(chain),
                "target_service": nm(t),
                "target_service_type": nodes_raw[t]["service_type"] or "-",
                "target_application": ta,
                "scope": "INTERNAL" if sa == ta else "CROSS-APP",
                "full_path": full,
            },
        })
    return nodes, edges


def _fetch_dynatrace_entities(config: ServerConfig, settings: Settings) -> list[dict]:
    """Fetch SERVICE / QUEUE / APPLICATION entities with relationships."""
    collector = build_collector(config, settings)
    base = config.url.rstrip("/")
    headers = {
        "Authorization": f"Api-Token {config.token}",
        "Accept": "application/json",
    }
    entities: list[dict] = []
    with collector._client(headers=headers) as client:  # type: ignore[union-attr]
        for etype in _DT_ENTITY_TYPES:
            url = f"{base}/api/v2/entities"
            params: dict = {
                "entitySelector": f'type("{etype}")',
                "fields": _DT_FIELDS,
                "pageSize": "500",
                "from": _DT_LOOKBACK,
                "to": "now",
            }
            while url:
                resp = collector._request_with_retries(  # type: ignore[union-attr]
                    client, "GET", url, params=params
                )
                if resp.status_code == 400 and etype != "SERVICE":
                    break  # entity type not supported on this cluster
                resp.raise_for_status()
                data = resp.json()
                entities.extend(data.get("entities", []))
                next_key = data.get("nextPageKey")
                params = {"nextPageKey": next_key} if next_key else {}
                if not next_key:
                    url = ""
    return entities


# ===========================================================================
# Persistence
# ===========================================================================


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
    node_ids = {str(n["external_id"]) for n in nodes}
    seen_edges: set[str] = set()
    for e in edges:
        if (
            e["from_external_id"] not in node_ids
            or e["to_external_id"] not in node_ids
        ):
            continue
        ext = str(e["external_id"])
        if ext in seen_edges:
            continue
        seen_edges.add(ext)
        db.add(
            TopologyEdge(
                source_platform=platform,
                source_instance=instance,
                external_id=ext,
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


# ===========================================================================
# Row shaping (matches the export sheets) — shared by the page and export API
# ===========================================================================


def load_topology(
    db: Session, platform: SourcePlatform, instance: str
) -> tuple[list[TopologyNode], list[TopologyEdge]]:
    """Load one instance's stored nodes + edges."""
    nodes = list(
        db.scalars(
            select(TopologyNode)
            .where(
                TopologyNode.source_platform == platform,
                TopologyNode.source_instance == instance,
            )
            .order_by(TopologyNode.name)
        ).all()
    )
    edges = list(
        db.scalars(
            select(TopologyEdge).where(
                TopologyEdge.source_platform == platform,
                TopologyEdge.source_instance == instance,
            )
        ).all()
    )
    return nodes, edges


#: Column order for the NNMi L2 connections export (matches NNMi's own CSV).
NNMI_L2_COLUMNS = [
    "name", "status", "endpoint1", "endpoint2", "endpoint3", "endpoint4", "interfaces",
]


def nnmi_connection_rows(edges: list[TopologyEdge]) -> list[dict]:
    """L2 connections shaped like the NNMi export (name/status/endpoints/ifs)."""
    seen: set[str] = set()
    rows: list[dict] = []
    for e in edges:
        a = e.attributes or {}
        name = a.get("name") or e.label or ""
        if name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "name": name,
                "status": a.get("status") or "",
                "endpoint1": a.get("endpoint1", ""),
                "endpoint2": a.get("endpoint2", ""),
                "endpoint3": a.get("endpoint3", ""),
                "endpoint4": a.get("endpoint4", ""),
                "interfaces": a.get("interfaces", ""),
            }
        )
    rows.sort(key=lambda r: r["name"])
    return rows


#: Unified_Map sheet columns (matches the Dynatrace export).
UNIFIED_COLUMNS = [
    ("source_application", "Source Application"),
    ("source_service", "Source Service"),
    ("source_service_type", "Source Service Type"),
    ("link_type", "Link Type"),
    ("middleware_chain", "Middleware Chain"),
    ("middleware_type", "Middleware Type"),
    ("mw_hops", "MW Hops"),
    ("target_service", "Target Service"),
    ("target_service_type", "Target Service Type"),
    ("target_application", "Target Application"),
    ("full_path", "Full Path"),
]


def dynatrace_unified_rows(edges: list[TopologyEdge]) -> list[dict]:
    """One row per resolved path — the Unified_Map shape."""
    rows = [dict(e.attributes or {}) for e in edges if e.kind == "path"]
    rows.sort(
        key=lambda r: (
            r.get("scope") != "CROSS-APP",
            (r.get("source_application") or "").lower(),
            (r.get("target_application") or "").lower(),
            (r.get("source_service") or "").lower(),
        )
    )
    return rows


def _link_kind(direct: int, via: int) -> str:
    return "Both" if direct and via else "Direct" if direct else "Via Middleware"


def dynatrace_app_rows(rows: list[dict]) -> list[dict]:
    """App→App aggregation of the unified rows (cross-application only)."""
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.get("scope") != "CROSS-APP":
            continue
        key = (r.get("source_application", ""), r.get("target_application", ""))
        a = agg.setdefault(key, {"direct": 0, "via": 0})
        a["via" if r.get("mw_hops") else "direct"] += 1
    out = [
        {
            "source_application": k[0],
            "target_application": k[1],
            "link_type": _link_kind(v["direct"], v["via"]),
        }
        for k, v in agg.items()
    ]
    out.sort(key=lambda r: (r["source_application"].lower(), r["target_application"].lower()))
    return out


def dynatrace_service_rows(rows: list[dict]) -> list[dict]:
    """Service→Service aggregation of the unified rows."""
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("source_service", ""), r.get("target_service", ""))
        a = agg.setdefault(key, {"direct": 0, "via": 0, "mw": set()})
        a["via" if r.get("mw_hops") else "direct"] += 1
        chain = r.get("middleware_chain") or "-"
        if chain and chain != "-":
            a["mw"].add(chain)
    out = [
        {
            "source_service": k[0],
            "target_service": k[1],
            "link_type": _link_kind(v["direct"], v["via"]),
            "middleware_chain": " / ".join(sorted(v["mw"])) or "-",
        }
        for k, v in agg.items()
    ]
    out.sort(key=lambda r: (r["source_service"].lower(), r["target_service"].lower()))
    return out


# ===========================================================================
# Mock data
# ===========================================================================


def _mock_network(instance: str) -> tuple[list, list]:
    slug = instance.lower().replace(" ", "-")
    devices = [
        ("core-01", "Router", "NORMAL"),
        ("core-02", "Router", "NORMAL"),
        ("dist-01", "Switch", "NORMAL"),
        ("dist-02", "Switch", "MAJOR"),
        ("acc-01", "Switch", "NORMAL"),
        ("acc-02", "Switch", "MINOR"),
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
    # (a, ifA, b, ifB, status)
    links = [
        ("core-01", "Bundle-Ether1", "core-02", "Bundle-Ether1", "NORMAL"),
        ("core-01", "Gi0/1", "dist-01", "Gi1/0/1", "NORMAL"),
        ("core-02", "Gi0/1", "dist-02", "Gi1/0/1", "MAJOR"),
        ("dist-01", "Gi1/0/2", "acc-01", "Gi1/0/24", "NORMAL"),
        ("dist-01", "Gi1/0/3", "acc-02", "Gi1/0/24", "MINOR"),
        ("dist-02", "Gi1/0/2", "acc-02", "Gi1/0/23", "NORMAL"),
        ("core-01", "Gi0/2", "fw-01", "eth1", "NORMAL"),
        ("fw-01", "eth2", "wan-01", "Gi0/0", "NORMAL"),
        ("core-02", "Gi0/2", "wan-01", "Gi0/1", "NORMAL"),
    ]
    edges = []
    for i, (a, ifa, b, ifb, st) in enumerate(links):
        ep1 = f"{a}.{slug}[{ifa}]"
        ep2 = f"{b}.{slug}[{ifb}]"
        name = f"{ep1},{ep2}"
        edges.append({
            "external_id": f"{slug}-l2-{i}",
            "from_external_id": f"{slug}-{a}",
            "to_external_id": f"{slug}-{b}",
            "kind": "l2",
            "label": name,
            "status": st,
            "attributes": {
                "name": name,
                "status": st,
                "endpoint1": ep1,
                "endpoint2": ep2,
                "interfaces": f"{600000000000 + i}",
            },
        })
    return nodes, edges


def _mock_service_map(instance: str) -> tuple[list, list]:
    """A small unified map: services grouped into apps, some via middleware."""
    slug = instance.lower().replace(" ", "-")
    # service -> (application, service_type, is_middleware, mw_category)
    services = {
        "portal-web": ("Portal", "WEB_REQUEST_SERVICE", False, ""),
        "portal-api": ("Portal", "WEB_SERVICE", False, ""),
        "order-api": ("Order Management", "WEB_SERVICE", False, ""),
        "provision-svc": ("Order Management", "WEB_SERVICE", False, ""),
        "billing-api": ("Billing", "WEB_SERVICE", False, ""),
        "rating-engine": ("Billing", "WEB_SERVICE", False, ""),
        "payment-api": ("Payments", "WEB_SERVICE", False, ""),
        "auth-svc": ("Identity", "WEB_SERVICE", False, ""),
        "orders-mq": ("<middleware>", "MESSAGING", True, "Messaging"),
        "billing-mq": ("<middleware>", "MESSAGING", True, "Messaging"),
        "oracle-crm": ("<middleware>", "DATABASE", True, "Database"),
    }
    nodes = []
    for name, (app, stype, is_mw, _cat) in services.items():
        if is_mw:
            continue  # middleware is transparent — shown only in the chain text
        nodes.append({
            "external_id": f"{slug}-svc-{name}",
            "kind": "service",
            "name": name,
            "category": stype,
            "status": None,
            "attributes": {"application": app, "service_type": stype, "technology": ""},
        })

    # (source, [middleware...], target)
    paths = [
        ("portal-web", [], "portal-api"),
        ("portal-api", [], "order-api"),
        ("portal-api", [], "auth-svc"),
        ("order-api", ["orders-mq"], "provision-svc"),
        ("order-api", [], "billing-api"),
        ("billing-api", ["billing-mq"], "rating-engine"),
        ("billing-api", ["oracle-crm"], "payment-api"),
        ("payment-api", ["oracle-crm"], "auth-svc"),
    ]
    edges = []
    for i, (s, chain, t) in enumerate(paths):
        sa = services[s][0]
        ta = services[t][0]
        mw_names = " → ".join(chain)
        mw_types = ", ".join(sorted({services[m][3] for m in chain}))
        full = (
            f"{sa} / {s}"
            + (f"  →[{mw_names}]→  " if chain else "  →  ")
            + f"{t} / {ta}"
        )
        edges.append({
            "external_id": f"{slug}-path-{i}",
            "from_external_id": f"{slug}-svc-{s}",
            "to_external_id": f"{slug}-svc-{t}",
            "kind": "path",
            "label": "Via Middleware" if chain else "Direct",
            "attributes": {
                "source_application": sa,
                "source_service": s,
                "source_service_type": services[s][1],
                "link_type": "Via Middleware" if chain else "Direct",
                "middleware_chain": mw_names or "-",
                "middleware_type": mw_types or "-",
                "mw_hops": len(chain),
                "target_service": t,
                "target_service_type": services[t][1],
                "target_application": ta,
                "scope": "INTERNAL" if sa == ta else "CROSS-APP",
                "full_path": full,
            },
        })
    return nodes, edges

