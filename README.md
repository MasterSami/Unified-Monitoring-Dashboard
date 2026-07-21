# Unified Monitoring Dashboard

A single web UI that aggregates hosts and alerts from **Zabbix**, **Dynatrace**,
and **NNMi** into one normalized view. Built as a local proof-of-concept for the
DC Admin Team, but structured to move onto an internal server with PostgreSQL
without code changes.

## Features

- **Overview** — KPI cards (total hosts, hosts down, active alerts), host counts
  per platform, active alerts broken out by severity (5 colored counters), and
  the last successful run per collector. Auto-refreshes every 60s.
- **Hosts** — unified inventory table with live search, platform filter tabs, and
  sortable columns. Status pills and platform badges.
- **Alerts** — active alerts sorted by severity then recency, colored severity
  pills, platform badges. Auto-refreshes every 60s.
- **Collector health strip** on every page — green/red dot per platform with a
  tooltip showing the last error when a collector is failing.
- **Dark mode** — navbar toggle, persisted in `localStorage`, follows
  `prefers-color-scheme` by default, no flash of the wrong theme.
- **JSON API** under `/api/v1` for reports and automation.
- **MOCK_MODE** — realistic fake data (30 hosts, 12 alerts) so the UI can be
  developed and demoed without VPN access to the real systems.

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
├── models.py          Host, Alert, CollectorRun
├── schemas.py         pydantic API schemas
├── normalizer.py      severity maps + upsert/reconcile logic
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
#    Leave MOCK_MODE=true to demo without VPN, or fill in the real
#    URLs/tokens and set MOCK_MODE=false.

# 5. Run it
uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

On first startup with an empty database, the app runs one collection
immediately so the UI has data, then continues polling on the schedule.

### Configuration reference (`.env`)

| Key                     | Meaning                                                  |
| ----------------------- | -------------------------------------------------------- |
| `DATABASE_URL`          | SQLAlchemy URL. Default `sqlite:///./dashboard.db`.      |
| `POLL_INTERVAL_MINUTES` | Background polling cadence.                              |
| `ENABLED_COLLECTORS`    | Comma list; disable a platform without touching code.    |
| `MOCK_MODE`             | `true` loads fake data; `false` calls the real systems.  |
| `TLS_VERIFY`            | `false` to allow internal self-signed certs.             |
| `ZABBIX_URL/TOKEN`      | Zabbix base URL and API token.                           |
| `DYNATRACE_URL/TOKEN`   | Dynatrace env URL (`.../e/ENV_ID`) and API token.        |
| `NNMI_URL/USER/PASS`    | NNMi base URL and SOAP credentials.                      |

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
| `GET  /api/v1/summary`                | Aggregate KPIs for the overview.                 |
| `GET  /api/v1/collectors/status`      | Per-collector health.                            |
| `POST /api/v1/collectors/{name}/run`  | Trigger one collector now (the UI "Refresh now").|

Interactive docs at `/docs`.

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
