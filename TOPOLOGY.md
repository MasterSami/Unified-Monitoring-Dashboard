# Topology / Service Map / App Map — design & enablement

Status: **planned / scaffolded, disabled by default.** This document is the plan
for adding network- and application-topology views to the dashboard. The UI
entry point exists (a disabled "Topology · soon" item and a placeholder page);
the extraction collectors are not built yet.

---

## What it will show

Three related maps, from the systems you already monitor:

1. **Network topology (NNMi)** — nodes, interfaces, IP addresses, and **L2
   connections** (which switch/router port connects to which), i.e. the
   physical/logical network graph from NNMi-13.
2. **Service map (Dynatrace)** — service→service call graph, resolving calls
   that flow **through middleware** (MQ / Kafka / ESB / API-gateway / DB).
3. **Application map (Dynatrace)** — the service graph rolled up to
   **business-application** level (App→App, direct or via middleware).

These mirror the three sample scripts you provided
(`ExportNnmiTopology.py`, `dynatrace_service_map.py`, `dynatrace_app_map.py`).

---

## How to enable the placeholder now

```
# .env
ENABLE_TOPOLOGY=true
```

Restart the app. The nav item becomes active and `/topology` opens the page.
Until the collectors below are implemented it shows an "under construction"
state. Leaving `ENABLE_TOPOLOGY=false` (default) hides the feature.

> To hide the "soon" nav item entirely, remove the `{% else %}` branch of the
> Topology block in `app/templates/base.html`.

---

## Build plan (when we turn it on)

### 1. Data model (`app/models.py`)

Add topology tables, scoped by `source_instance` like everything else:

- `TopologyNode(id, source_platform, source_instance, external_id, kind, name,
  ip, attributes JSON, updated_at)` — a node (NNMi node, Dynatrace service, app).
- `TopologyEdge(id, source_platform, source_instance, from_external_id,
  to_external_id, kind, via JSON, attributes JSON, updated_at)` — a link
  (L2 connection, service call, app→app), `via` holding any middleware chain.

### 2. Collectors (`app/collectors/`)

Reuse the collector base and the proven extraction logic from the scripts:

- **NNMi** (`nnmi.py`, extend): add `NodeBean` (have it), `InterfaceBean`,
  `IPAddressBean`, `L2ConnectionBean` using the same `filt:expression` SOAP
  envelope already in the collector. L2 connection `name` is
  `NodeA[ifA],NodeB[ifB]` — split into edges.
- **Dynatrace** (`dynatrace.py`, extend): Entities v2 with
  `fields=+fromRelationships,+toRelationships,+properties,+managementZones,+tags`
  for `SERVICE` (and `QUEUE`, `APPLICATION`). Port the middleware classification
  and path-resolution from `dynatrace_service_map.py`, and the 4-layer
  application-grouping engine from `dynatrace_app_map.py`.

Run these on a **slower schedule** than hosts/alerts (topology changes rarely) —
e.g. a separate hourly job — to keep polling light.

### 3. API (`app/routers/api.py`)

- `GET /api/v1/topology/nodes` and `/edges` (filter by platform/instance/kind).
- `GET /api/v1/topology/graph?view=network|service|app` → nodes+edges JSON for
  the renderer.
- CSV/Excel export of the maps (the scripts already produce Excel + Mermaid;
  reuse that shape).

### 4. UI (`app/templates/topology.html`)

- A view switcher: **Network / Service / Application**.
- Render the graph. Options, lightest first:
  - **Mermaid** (already supported in this repo's docs) for small/medium graphs —
    generate the diagram server-side like the scripts do.
  - A vendored graph library (e.g. Cytoscape.js) for large, interactive,
    zoomable graphs — vendored locally to keep the no-CDN rule.
- Filters: by management zone / device category / instance; search a node and
  highlight its neighbors; export the current view.

### 5. Scale note

The Dynatrace environment is large (tens of thousands of entities). Fetch with
pagination (`nextPageKey`), cache the computed graph in the topology tables, and
render a filtered/limited subgraph in the browser rather than the whole graph at
once.

---

## Why it's staged

Topology extraction is a substantial subsystem (SOAP bean-by-bean for NNMi, plus
Dynatrace relationship-graph building, middleware resolution, and application
grouping). Shipping it disabled keeps the dashboard stable while the collectors
are built and tuned against the real environments — the same iterative approach
that got hosts and alerts working.
