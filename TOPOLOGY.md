# Topology / Service Map — design & enablement

Status: **built, disabled by default.** The topology feature is fully
implemented — data model, collectors, JSON API, and a UI with both a **table**
view and an interactive **connection-graph** view. It ships **off** behind
`ENABLE_TOPOLOGY` so you turn it on yourself when you want it.

---

## What it shows

Two maps, extracted from the systems you already monitor:

1. **Network topology (NNMi)** — devices (nodes) and their **L2 connections**
   (which switch/router connects to which), i.e. the physical/logical network
   graph. Nodes are colored by NNMi status (Normal / Warning / Major).
2. **Service map (Dynatrace)** — the service→service **call graph**, with
   databases and message queues shown as middleware. Directional arrows show
   who calls whom — the same shape as the Dynatrace service map.

Each map is viewable two ways:

- **Graph** — an interactive, zoomable, draggable node/edge diagram
  (Cytoscape.js, vendored locally — no CDN).
- **Table** — the same data as browsable rows, shaped to match the source
  exports:
  - **NNMi** → an *L2 connections* table: `name, status, endpoint1..4,
    interfaces` (identical to the `NNMI_l2_connections.csv` export).
  - **Dynatrace** → three sub-views matching the inventory workbook:
    **Unified map** (`Source App / Service → [middleware chain] → Target
    Service / App`), **App → App**, and **Service → Service**. Middleware
    (MQ / ESB / DB) is classified and made transparent, and services are grouped
    into business applications by tag → name pattern → management zone → RUM —
    a port of `dynatrace_unified_map.py`.

### Export

With `ENABLE_TOPOLOGY_EXPORT=true` (separate switch, off by default) the table
views get an **Export** button:

- **NNMi** → `nnmi_l2_connections.csv` (the L2 columns above).
- **Dynatrace** → `dynatrace_unified_map.xlsx` with three sheets —
  `Unified_Map`, `App_to_App`, `Service_to_Service` — the same shape as the
  provided `Dynatrace_Inventory_Map.xlsx`.

API: `GET /api/v1/topology/nnmi-l2.csv?instance=` and
`GET /api/v1/topology/dynatrace-map.xlsx?instance=` (both 404 when the flag is
off).

> **Application map** (services rolled up to business-application level) is a
> natural next step and is intentionally **not** built yet — the data model and
> UI generalize to it (add a `kind="application"` node + rollup edges).

---

## How to enable it

```
# .env
ENABLE_TOPOLOGY=true
```

Restart the app. The **Topology** nav item becomes active and `/topology`
opens the views. On startup (and then on a slow interval) the topology is
extracted for every configured NNMi and Dynatrace instance. In `MOCK_MODE`
a representative sample graph is seeded so you can see the feature immediately.

Leaving `ENABLE_TOPOLOGY=false` (the default) hides the nav item, serves a
"turned off" placeholder at `/topology`, and returns `404` from the topology
API — nothing is collected.

Use the **↻ Rebuild** button on the page to re-extract on demand.

---

## How it's built

### 1. Data model (`app/models.py`)

Two tables, scoped by `source_instance` like everything else:

- `TopologyNode(source_platform, source_instance, external_id, kind, name,
  category, status, attributes JSON)` — a device (NNMi) or service/middleware
  (Dynatrace). Unique on `(platform, instance, external_id)`.
- `TopologyEdge(source_platform, source_instance, external_id,
  from_external_id, to_external_id, kind, label, attributes JSON)` — an L2 link
  (`kind="l2"`) or a service call (`kind="call"`).

### 2. Collectors (`app/topology.py`)

Extraction recipes learned from the provided export scripts:

- **NNMi** — `NodeBean` (`getNodes`) for devices and `L2ConnectionBean`
  (`getL2Connections`) for links, over the same `filt:expression` SOAP envelope
  the host collector already uses. The L2 connection `name` is
  `NodeA[ifA],NodeB[ifB]` — parsed and mapped back to node IDs to build edges.
- **Dynatrace** — Entities v2 for `SERVICE` with
  `fields=+fromRelationships,+toRelationships,+properties`, paginated by
  `nextPageKey`. `calls` relationships become directed edges; only edges whose
  endpoints are known services are kept.

`run_topology()` replaces each instance's graph as a **snapshot** (delete +
insert) on every run — topology changes rarely, so this is simpler and safe.
It runs on a **slower schedule** than hosts/alerts (a separate job, ~6× the
poll interval, min 30 min) plus once on startup.

### 3. API (`app/routers/api.py`)

- `GET /api/v1/topology/graph?view=network|service&instance=<name>` →
  Cytoscape elements JSON (nodes + edges), gated by `ENABLE_TOPOLOGY`.
- `POST /api/v1/topology/run` → rebuild all instances now.

### 4. UI (`app/templates/topology.html`)

- View switcher (**Network / Service**), an instance dropdown, and a
  **Graph / Table** toggle — all plain links/selects, so state lives in the URL
  (`/topology?view=…&instance=…&mode=…`).
- Graph rendered by vendored `app/static/cytoscape.min.js`; node colors read the
  app's CSS variables so the graph respects light/dark mode. A legend explains
  the colors.

### 5. Scale note

The Dynatrace environment can be large (tens of thousands of entities). The
collector paginates via `nextPageKey` and the computed graph is cached in the
topology tables; the browser renders one instance's subgraph at a time. If a
single instance's graph is still very large, filter it further before rendering
(by management zone / category) — the data model already supports it via
`attributes`.
