# Unified Monitoring Dashboard

> 📐 [`ARCHITECTURE.md`](ARCHITECTURE.md) — technology & design writeup · ▶️ [`HOW_TO_RUN.md`](HOW_TO_RUN.md) — run & maintenance commands

A single web UI that aggregates hosts and alerts from **Zabbix**, **Dynatrace**,
and **NNMi** into one normalized view. Built as a local proof-of-concept for the
DC Admin Team, but structured to move onto an internal server with PostgreSQL
without code changes.

## Features

- **Overview** — KPI cards (total hosts, hosts down, active alerts), host counts
  per platform, active alerts broken out by severity (5 colored counters), and
  the last successful run per collector. Auto-refreshes every 60s.
- **Hosts** — unified inventory table with live search (hostname, IP, group, or
  instance), platform filter tabs, and sortable columns. Status pills (up / down
  / unknown / disabled) and platform badges.
- **Service / group filter** — on Capacity, Agents and Alerts alike. Operations
  teams know a server by the tool it belongs to ("Billing", "CRM"), not by its
  IP, so the same box on all three pages offers the group names seen in the
  sources and matches by substring — typing `bill` is enough. The list follows
  the platform tab and instance you have picked (the Dynatrace tab offers only
  Dynatrace host groups); under "All" each name shows which tool it came from.
  The menu is drawn by the app, so it looks the same in Firefox and Chrome. For Zabbix
  that is the host group (a host in several groups matches each of them); for
  Dynatrace the host group or group tag; for NNMi the device category; for
  SiteScope the monitor group. Alerts inherit the group of their host. The
  filter carries into the Excel / CSV exports (`group=`).
- **Shared devices** — devices monitored by more than one instance, correlated
  by IP (e.g. the same node on Zabbix-34 *and* Zabbix-67, or on Zabbix and NNMi).
- **Pagination & CSV export** — hosts and alerts paginate (300/page, configurable
  via `PAGE_SIZE`) and export the current filtered view to CSV.
- **Capacity planning** — a **Planning** tab under Capacity listing the volumes that are
  filling up and roughly when they reach 90%, from a least-squares trend over
  each drive's recent history. Sortable by time-to-threshold, with an inline
  sparkline, a confidence rating, and a "Capacity risks" tile on the Overview.
  See [Capacity planning](#capacity-planning) for the method and its
  limits.
- **Topology (planned)** — a feature-flagged placeholder for NNMi network
  topology and Dynatrace service/app maps; see [`TOPOLOGY.md`](TOPOLOGY.md).
- **Alerts** — active alerts sorted by severity then recency, colored severity
  pills, platform badges. Auto-refreshes every 60s.
- **Collector health strip** on every page — green/red dot per platform with a
  tooltip showing the last error when a collector is failing.
- **Dark mode** — navbar toggle, persisted in `localStorage`, follows
  `prefers-color-scheme` by default, no flash of the wrong theme.
- **JSON API** under `/api/v1` for reports and automation.
- **Multiple servers per platform** — declare as many Zabbix / Dynatrace / NNMi
  instances as you have in `servers.yaml`; each is polled and tracked
  independently and tagged by instance throughout the UI and API.
- **Zabbix extras** — username/password *or* API-token auth; optional proxy
  fleet health per instance; a "Send test mail" button that asks Zabbix to send
  a test email (verifies email alerting end-to-end).
- **MOCK_MODE** — realistic fake data across several fake instances so the UI
  can be developed and demoed without VPN access to the real systems.

## Architecture

| Concern            | Choice                                                    |
| ------------------ | --------------------------------------------------------- |
| Web framework      | FastAPI + Uvicorn                                         |
| ORM / DB           | SQLAlchemy 2.x — SQLite now, PostgreSQL later (same code) |
| Background polling | APScheduler (interval job)                                |
| Templating / UI    | Jinja2 + HTMX + vanilla CSS (no build step)               |
| HTTP / SOAP        | httpx (REST + raw SOAP envelopes), lxml for parsing       |
| Config             | pydantic-settings (`.env`)                                |

Each collector normalizes its platform's payloads into a shared `Host` / `Alert`
model. Collectors are fully isolated: **one platform being down never affects the
others or the web UI** — failures are caught, logged, and recorded on a
`CollectorRun` row.

```
app/
├── main.py            FastAPI app, startup, scheduler wiring
├── config.py          pydantic-settings (.env)
├── db.py              engine, session, Base
├── models.py          Host, Alert, CollectorRun, CapacityHistory, CapacityForecast
├── schemas.py         pydantic API schemas
├── normalizer.py      severity maps + upsert/reconcile logic
├── capacity_history.py  capacity sampling + drive-string parsers
├── forecast.py        least-squares trend fits + classification
├── backfill_zabbix_capacity.py  one-off history backfill from Zabbix trends
├── scheduler.py       APScheduler jobs + per-collector status
├── collectors/
│   ├── base.py        BaseCollector (timing, upsert, run record)
│   ├── zabbix.py      JSON-RPC 2.0
│   ├── dynatrace.py   Entities v2 + Problems v2 (403-graceful)
│   ├── nnmi.py        SOAP (NodeBean / IncidentBean)
│   └── mock_data.py   MOCK_MODE fixtures
├── routers/
│   ├── pages.py       HTML pages + HTMX partials
│   └── api.py         JSON API under /api/v1
├── templates/         Jinja2 (base, overview, hosts, alerts, partials/)
└── static/            style.css, theme.js, htmx.min.js (vendored)
```

## Setup (local POC)

Requires **Python 3.11+**.

```bash
# 1. Enter the project
cd Unified-Monitoring-Dashboard

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env from the template
cp .env.example .env
#    Leave MOCK_MODE=true to demo without VPN.

# 5. (For live data) declare your servers
cp servers.example.yaml servers.yaml
#    Fill in URLs/credentials, then set MOCK_MODE=false in .env.
#    servers.yaml is gitignored — it holds credentials.

# 6. Run it
uvicorn app.main:app --reload
```

On Windows (PowerShell) use `copy` instead of `cp`.

Then open <http://127.0.0.1:8000>.

On first startup with an empty database, the app runs one collection
immediately so the UI has data, then continues polling on the schedule.

### Configuration reference (`.env`)

| Key                     | Meaning                                                  |
| ----------------------- | -------------------------------------------------------- |
| `DATABASE_URL`          | SQLAlchemy URL. Default `sqlite:///./dashboard.db`.      |
| `POLL_INTERVAL_MINUTES` | Background polling cadence.                              |
| `ENABLED_COLLECTORS`    | Comma list; disable a platform without touching config.  |
| `MOCK_MODE`             | `true` loads fake data; `false` calls the real systems.  |
| `TLS_VERIFY`            | `false` to allow internal self-signed certs.             |
| `SERVERS_CONFIG`        | Path to the server inventory YAML (default `servers.yaml`). |
| `TEST_MAIL_TO`          | Default recipient for the Zabbix "Send test mail" button. |

### Server inventory (`servers.yaml`)

Connection details live here (kept out of git). Add as many instances per
platform as you have; each needs a unique `name`:

```yaml
zabbix:
  - name: Zabbix-A
    url: "https://zabbix-a.example.local:8443"
    user: "your.user"          # or use `token:` for a static API token
    password: "your-password"
    check_proxies: true        # show proxy fleet health for this instance
    test_mail: true            # show a "Send test mail" button

dynatrace:
  - name: Dynatrace-Prod
    url: "https://xxxxx.dynatrace-managed.com/e/ENV_ID/"
    token: "dt0c01...."

nnmi:
  - name: NNMi-A
    url: "https://nnmi-a.example.local/nnm/"
    user: "your.user"
    password: "your-password"
```

- **Zabbix auth**: provide either `user`+`password` (the collector calls
  `user.login`) or a static `token`.
- **check_proxies**: for Zabbix instances, the collector also queries
  `proxy.get` and surfaces "N/M online" (with any offline proxies named) on the
  instance's card and health tooltip.
- **test_mail**: exposes a button that reads the instance's Email media-type
  SMTP settings and sends a test email to `TEST_MAIL_TO` (or `?to=` on the API)
  through that relay via `smtplib`, verifying email alerting.
- **verify_tls**: optional per-server override of the global `TLS_VERIFY`.

> **Note on Dynatrace scopes.** The dashboard uses the Entities v2 API for
> hosts (needs `entities.read`). If the token lacks `problems.read`, the
> Problems v2 call returns 403 — this is handled gracefully: the alerts feed is
> marked _"unavailable — token lacks problems.read scope"_ and collection
> continues.

## JSON API

| Method & path                         | Purpose                                          |
| ------------------------------------- | ------------------------------------------------ |
| `GET  /api/v1/hosts`                  | All hosts (filters: `platform`, `status`, `q`).  |
| `GET  /api/v1/alerts?active=true`     | Alerts; `active=true` hides resolved.            |
| `GET  /api/v1/agents.xlsx`            | Agents export (`platform`, `instance`, `group`, `status`, `q`). |
| `GET  /api/v1/capacity.xlsx` / `.csv` | Capacity export (`platform`, `instance`, `group`, `status`, `q`). |
| `GET  /api/v1/alerts.xlsx` / `.csv`   | Alerts export (`state`, `group`, `q`, `date_from`, `date_to`). |
| `GET  /api/v1/forecast`               | Capacity forecast (`classification`, `kind`, `platform`, `instance`, `group`, `q`). |
| `GET  /api/v1/forecast.xlsx`          | Forecast export (same filters).                  |
| `POST /api/v1/forecast/run`           | Refit every series now.                          |
| `GET  /api/v1/summary`                | Aggregate KPIs for the overview.                 |
| `GET  /api/v1/collectors/status`      | Per-instance health.                             |
| `POST /api/v1/collectors/run`         | Trigger every instance now (the UI "Refresh now").|
| `POST /api/v1/collectors/{instance}/run` | Trigger one instance now.                     |
| `POST /api/v1/collectors/{instance}/test-mail?to=` | Zabbix: send a test email.        |
| `GET  /api/v1/events`                 | Canonical events — full Alert schema (filters: `platform`, `instance`, `entity_id`, `resolved`, `q`). |
| `GET  /api/v1/entities`               | Resolved canonical entities (filters: `entity_type`, `q`). |
| `GET  /api/v1/entities/{id}`          | One entity: aliases, IPs, every source row resolved to it. |
| `GET  /api/v1/entity-mappings`        | Source-identifier -> entity, manual + automatic (filters: `entity_id`, `platform`). |
| `POST /api/v1/entity-mappings`        | Declare an explicit source-identifier -> entity mapping. |

Interactive docs at `/docs`.

## Capacity planning

The **Capacity → Planning** tab (`/capacity/planning`) answers one question — *which volumes are going to fill
up, and roughly when* — and is deliberate about the cases where it refuses to
answer.

### How it works

1. **Sampling.** Every collection run appends its capacity readings to a
   `capacity_history` table, one row per `(host, metric, drive)` per sample.
   The `hosts` table only ever holds the latest reading, so before this table
   existed nothing in the schema could answer "is this filling up?". Sampling
   is throttled to one row per host per `CAPACITY_HISTORY_MIN_MINUTES`
   (default hourly), because the forecast resamples to one point per day and
   storing all 288 readings of a five-minute poll cycle would cost twelve times
   the rows for no extra resolution.
2. **Fitting.** A nightly job at **03:30** resamples each series to a daily
   mean, fits an ordinary least-squares line to `used_pct` against days over
   the last `FORECAST_WINDOW_DAYS` (default 30), and stores slope, R², current
   value and the resulting dates in `capacity_forecast`.
3. **Reading.** The pages read that table only. Nothing is fitted on page load.

Trigger a refit without waiting for the small hours with **Recompute now** on
the page, or `POST /api/v1/forecast/run`.

### When the page is empty

The page says which of the possible reasons applies, and this reports the
same thing in more detail without touching anything:

```bash
python -m app.backfill_zabbix_capacity --status
```

It prints the database actually in use (a relative SQLite path resolves
against the working directory, so running the app and a command from
different folders silently uses different files), the stored samples per
platform with their date range, the forecast rows per classification, and how
many hosts pass the staleness gate.

### A note on CPU percentages

Some Zabbix templates, the VMware ones in particular, report CPU summed across
vCPUs the way `top` does: a busy 2-vCPU guest reads 130%. Those hosts usually
carry two items, **CPU utilization** (normalized 0-100) and **CPU usage in
percent** (summed), and the dashboard reads the first. Where only a summed
reading exists it is divided by the vCPU count; anything still above 100 after
that is capped, and the original figure is shown on the Capacity detail panel
so it can be reconciled against Zabbix.

### What the number means, and what it does not

**The model is a straight line.** That is the whole of it. It is a good
description of a log directory that grows with steady traffic, or a database
that ingests at a constant rate. It is a poor description of everything else,
so the code is built to say so rather than to guess:

- **Low-confidence forecasts are suppressed.** If a series has an R² below
  `FORECAST_MIN_R_SQUARED` (default 0.3), the points do not lie on a line, and
  a date extrapolated from that line would be invented precision — so the date
  is withheld and only the slope is shown. A confident wrong date is worse than
  no date, because it is precise enough to schedule against.

  The R² test runs *after* the timescale test, not before it, and only changes
  the verdict for series that would otherwise land in an actionable band. A
  series crossing 90% more than 90 days out is `ok` whatever its R², because
  "not filling up on a timescale you care about" holds whether or not the
  points sit on a line. Only a series crossing *within* 90 days is downgraded
  to **noisy**. Without that ordering, every idle host's memory — flat, jittery,
  nominally trending at a hundredth of a percent a day — would be reported as
  noisy, and the list would be too long to read.
- **Step changes invalidate the history before them.** A cleanup that frees
  40%, a new workload that adds 20% overnight, a migration — after any of
  these, the samples from before the step describe a system that no longer
  exists, and a line fitted across the step is meaningless. The only step the
  code detects automatically is a **volume resize**: when `total_value` moves
  by more than `FORECAST_RESIZE_TOLERANCE` (default 5%) mid-window, only the
  samples after the most recent change are used, and the row says so. A
  cleanup or a workload change is *not* detected — the trend will simply be
  wrong until enough new history accumulates to outweigh the old.
- **Series that cannot support a trend are skipped, not guessed at.** Fewer
  than `FORECAST_MIN_POINTS` daily points, a span under
  `FORECAST_MIN_SPAN_DAYS`, a host whose status is `unknown`/`disabled` or that
  has not been seen for `FORECAST_STALE_AFTER_DAYS` (default 10), or a volume
  reporting zero size: each is recorded as **insufficient_data** with the
  reason shown in the row. The staleness window is deliberately wide, because
  `last_seen` only advances while the app runs: a dashboard switched off over
  a weekend would otherwise gate out the entire estate on Monday, which reads
  as lost data rather than as stale hosts.
- **CPU is sampled but never forecast.** A CPU percentage oscillates around a
  workload; it does not accumulate. "Days until CPU is 90%" is a category
  error, so only disk and memory are fitted.
- **An ETA past ten years is dropped.** At that range the arithmetic is not a
  forecast.
- **A volume already at or over 90% is reported, not forecast.** It is
  classified `critical` with "now", whatever its slope — including flat or
  falling. Two reasons. First, an ETA is `headroom ÷ slope` and both shrink to
  nothing as a disk fills: a volume at 99.99% creeping at 0.0008 %/day divides
  0.01 by 0.0008 and announces "13 days to full", a number built entirely from
  digits too small to display, which the next poll would move to 400 days or
  to 3. Under `MIN_HEADROOM_PCT` (0.5 percentage points) the answer is "now".
  Second, a volume sitting at 97% for a month is the most urgent row on the
  page; classifying it `ok` because it stopped growing would bury the one
  thing somebody has to deal with today.

### Classifications

| Class | Meaning |
| ----- | ------- |
| `critical` | Already at 90%+, or reaches it in under 14 days |
| `warning` | 14–45 days |
| `watch` | 45–90 days |
| `ok` | Over 90 days, flat, or shrinking |
| `noisy` | Rising, but R² too low to put a date on it |
| `insufficient_data` | Not fitted; the row states why |

### Backfilling history from Zabbix

Sampling only starts accumulating the day it ships, so a fresh install has
nothing to fit for weeks. Zabbix already keeps daily aggregates in `trends`
for exactly the items the Capacity page reads, so that history can simply be
pulled.

**This happens by itself.** After the first collection the app checks how much
Zabbix history each instance has; any instance with less than
`FORECAST_MIN_SPAN_DAYS` gets `CAPACITY_AUTO_BACKFILL_DAYS` (90) pulled in a
background job, and the forecast runs straight after. It is checked per
instance, so a Zabbix server added later is backfilled even though the others
already have history, and it stands down once an instance has enough - a poll
every five minutes never re-triggers a job that takes half an hour. Set
`CAPACITY_AUTO_BACKFILL=false` to do it by hand instead.

The same work is available as a command, for a first run you want to watch or
for re-pulling a longer window:

```bash
python -m app.backfill_zabbix_capacity --days 90
python -m app.backfill_zabbix_capacity --days 90 --instance Zabbix-A --forecast
```

It is idempotent (keyed on host + metric + drive + day), resumable (committed
per batch), and paced between batches. `--forecast` runs the fit immediately
afterwards instead of waiting for 03:30. Hosts are queried a hundred at a
time: asking per host cost two round trips each, which on a fifteen-thousand
host estate came to roughly thirty thousand requests and made a full run take
most of a day.

Two read-only modes help when a run does not produce what you expected:

```bash
python -m app.backfill_zabbix_capacity --probe    # trace three hosts, in seconds
python -m app.backfill_zabbix_capacity --status   # what the forecast can see
```

`--probe` prints, per host, the capacity items matched, the filesystems
classified, how many items have trend data, and the span of days returned - so
"Zabbix keeps no trends for these items" is distinguishable from "the run
never got that far" without waiting for a full pass.

**Dynatrace backfill** is available behind `--dynatrace` /
`FORECAST_DYNATRACE_BACKFILL=true` but usually fails: the Metrics v2 API needs
the `metrics.read` scope that the dashboard's token typically lacks — the same
403 the capacity collector already degrades around. It logs the reason and
skips; forecasting continues from whatever history exists.

### Trying it without a VPN

With `MOCK_MODE=true` each mock host is given 35 days of synthetic history
ending exactly on the value its Capacity row shows, covering every outcome —
one drive filling at ~0.8%/day, one flat, one shrinking, one too noisy to
date, and one resized mid-window (fitted only from the resize onward). The
series are digest-derived rather than random, so the demo has the same shape
on every run.

## Missing Dynatrace hosts

Dynatrace's `/api/v2/entities` API — the one the collector reads its host list
from — is time-windowed: a HOST entity that hasn't reported within the
requested window is simply absent from the response, no error, no count of
what was left out. A host that reports only intermittently, or one that
stopped reporting a while ago without being formally decommissioned in
Dynatrace, can be visible in Dynatrace's own UI while quietly never reaching
SAMI'X.

The collector now always sends an explicit window —
`DYNATRACE_ENTITY_LOOKBACK_DAYS` (default 370) — rather than relying on
whatever Dynatrace's own default happens to be. If hosts are still missing:

```bash
python -m app.dynatrace_probe --ip 10.22.68.11 10.22.68.12
python -m app.dynatrace_probe --ip-range 10.22.68.11-10.22.68.20
```

For each address, this queries every configured Dynatrace instance twice —
once the way the collector used to (no explicit window) and once with a wide
one — and reports which of three situations applies:

- **Found only with the wide window.** Confirms the time-window cause above.
  The current collector should already be past this; if not, check
  `DYNATRACE_ENTITY_LOOKBACK_DAYS` is actually set where the app reads it.
- **Found either way.** Not a time-window problem. A current build should
  already be collecting this host — if the dashboard still doesn't show it,
  wait for the next poll or trigger one from the Overview page.
- **Found nowhere, even over two years.** Not visible to this API token at
  all right now. Check the token's `entities.read` scope, whether this tenant
  restricts the token to specific management zones, and whether the address
  actually belongs to a different Dynatrace environment than the one
  configured in `servers.yaml`.

It also prints whether the queried address is a host's primary IP or a
secondary one (Dynatrace reports every NIC on a multi-homed host), and
whether SAMI'X already has that host stored under a *different* address on
the same machine — see the next section.

### Searching by a secondary IP

A multi-homed host only ever had its **first** reported address stored
anywhere, so a search for any other address it also answers to found nothing
— the host was not missing from the database, it was just indexed under an
address nobody happened to search for. Every address Dynatrace reports for a
host is now kept (comma-joined, the same convention `group_name` already uses
for Zabbix's multi-group hosts) and searched alongside the primary one; the
primary is still what the table displays.

## Correlation — Phase 1: canonical events + entity resolution

The same physical host is monitored by more than one tool under more than one
name — Zabbix's `APP01`, Dynatrace's `app-prod-01`, NNMi's `APP01_TE`. Phase 1
gives every one of those a shared, deterministic identity, as the foundation
a later correlation/incident phase builds on. **No AI/ML, no fuzzy matching —
every match is one of a fixed, ordered list of exact-match methods.**

- `app/models.py` — `Alert` grew the full canonical-event schema (resolved
  entity, preserved original severity/description, metric/trace/application
  context — most of the last group is schema-ready but unpopulated until a
  later phase produces that data; see the field comments). Four new tables —
  `CanonicalEntity`, `EntityIP`, `EntityAlias`, `EntityManualMapping` — hold
  the resolved identities themselves.
- `app/entity_resolution.py` — the matching algorithm, checked strongest
  evidence first: an explicit admin mapping, then a CMDB id, then an exact
  IP, FQDN, hostname, a registered alias, and finally "this exact source row
  resolved to something before, keep that". A bare hostname is never trusted
  ahead of a stronger signal — two unrelated hosts sharing a name across
  environments is common enough that the priority order itself is the
  safeguard. If the evidence points at two *different* existing entities
  (e.g. two entities both already claim the same IP — a real data-quality
  condition), resolution returns a `conflict` rather than guessing.
- `app/canonical_event.py` — one small adapter per source
  (Zabbix/Dynatrace/NNMi/SiteScope) mapping that platform's own payload onto
  the handful of canonical fields it doesn't already carry (an event-type
  label, tags, a problem category where the source actually has one). Source
  parsing quirks stay here, not in the resolver.
- Wired into the existing collector pipeline, not a parallel one: entity
  resolution runs inside `app.normalizer.upsert_hosts` (and the SiteScope
  push path's own upsert), batched — a whole poll's worth of new/changed
  hosts resolves in a handful of queries, not one per host, and a
  steady-state poll where nothing's identity changed costs none at all.
  Alerts inherit their host's resolved entity, IP, and owner at write time.

Read the result via:

```bash
GET /api/v1/events                 # the canonical Alert rows, full schema
GET /api/v1/entities                # resolved entities (filter: entity_type, q)
GET /api/v1/entities/{id}           # one entity: aliases, IPs, every source row
GET /api/v1/entity-mappings         # source-identifier -> entity, manual + automatic
POST /api/v1/entity-mappings        # declare an explicit mapping (e.g. an
                                     # identifier no shared IP/hostname/FQDN ties
                                     # to anything, so it needs a stated answer)
```

Correlating alerts into incidents, walking the topology graph, and root-cause
ranking are explicitly out of scope for this phase — they build on this
identity layer rather than reinventing it.

## Deploy to a server later

The application is deployment-ready; two changes move it from POC to server.

**1. Switch to PostgreSQL.** No code changes — just the URL and driver:

```bash
pip install "psycopg[binary]"
# in .env:
DATABASE_URL=postgresql+psycopg://dashboard:secret@db-host:5432/dashboard
MOCK_MODE=false
TLS_VERIFY=false   # or true if the monitoring systems use trusted certs
```

Tables are created automatically on startup (`Base.metadata.create_all`). For a
production change history you would add Alembic migrations, but it is not
required to run.

**2. Run under systemd.** Example unit:

```ini
# /etc/systemd/system/unified-dashboard.service
[Unit]
Description=Unified Monitoring Dashboard
After=network.target postgresql.service

[Service]
Type=simple
User=dashboard
WorkingDirectory=/opt/unified-dashboard
EnvironmentFile=/opt/unified-dashboard/.env
ExecStart=/opt/unified-dashboard/.venv/bin/uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now unified-dashboard
```

> Run a **single worker**. The APScheduler poller lives in-process; multiple
> Uvicorn workers would each start their own scheduler and poll in parallel. If
> you need multiple web workers, run the poller as a separate process/service.
> Put Nginx or Apache in front for TLS termination.

## Development notes

- **Type hints** throughout; docstrings on public functions.
- **Structured logging** via the stdlib `logging` module with per-collector
  logger names (`collector.zabbix`, etc.).
- Adding a platform: implement a `BaseCollector` subclass, add the severity map
  to `normalizer.py`, and register it in `collectors/__init__.py`.
