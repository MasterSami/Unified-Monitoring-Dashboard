# How to Run & Maintain — Unified Monitoring Dashboard

A practical command reference for running and operating the dashboard on
**Windows (PowerShell)**. Linux/WSL equivalents are noted where they differ.

> The app URL is always **http://127.0.0.1:8000**. Stop it any time with **Ctrl + C**.

---

## 0. One-time: install Python 3.12

The pinned dependencies ship prebuilt wheels for Python **3.12** (avoid 3.13/3.14).
Install from <https://www.python.org/downloads/release/python-31210/> and tick
**"Add python.exe to PATH"**.

If your machine uses the **Python Install Manager**, install 3.12 with:

```powershell
$py = "C:\Users\<you>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.PythonManager_3847v3x7pw1km\py.exe"
& $py install 3.12
& $py -3.12 --version
```

Use `& $py -3.12` wherever the steps below say `python`.

---

## 1. First-time setup

```powershell
# from inside the project folder
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# config files
copy .env.example .env
copy servers.example.yaml servers.yaml
```

- Edit **`.env`**: set `MOCK_MODE=false` for live data, `TEST_MAIL_TO=you@company.com`.
- Edit **`servers.yaml`**: add your Zabbix / Dynatrace / NNMi instances.
  YAML is indentation-sensitive — use **2 spaces** before `- name`, **4 spaces**
  for the fields under it, and **never tabs**.

Linux/WSL: use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp` instead of `copy`.

---

## 2. Run it

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app
```

Then open **http://127.0.0.1:8000**. Live-reload while editing code:

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

You should see `collector.<platform>.<instance>: collection complete: N hosts, M alerts`
in the log. Green dots in the sidebar mean an instance connected.

---

## 3. Stop it

- Press **Ctrl + C** in the terminal, or just close the PowerShell window.
- Stopping the app stops all polling — nothing runs in the background afterward.

---

## 4. Update to the latest code

If you cloned with git:

```powershell
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt   # in case deps changed
```

If you downloaded a ZIP (no git): download the new ZIP into a fresh folder, then
copy your existing config over so you don't re-enter it:

```powershell
copy "<old-folder>\.env" .env
copy "<old-folder>\servers.yaml" servers.yaml
```

After updating, do a browser **hard refresh once**: **Ctrl + F5** (assets are
version-stamped, so this is only needed the first time after a big UI change).

---

## 5. Reset the database

The SQLite database is `dashboard.db`. Delete it to clear all collected data;
it is recreated and repopulated on the next start.

```powershell
# stop the app first (Ctrl + C), then:
Remove-Item dashboard.db -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m uvicorn app.main:app
```

Do this whenever data looks stale or after switching between MOCK_MODE and live.

---

## 6. Common configuration changes (`.env`)

| Want to… | Change |
| --- | --- |
| Use fake demo data | `MOCK_MODE=true` |
| Use real systems | `MOCK_MODE=false` (needs `servers.yaml`) |
| Poll less often (lighter load) | `POLL_INTERVAL_MINUTES=15` |
| Allow self-signed certs | `TLS_VERIFY=false` |
| Disable a whole platform | `ENABLED_COLLECTORS=zabbix,nnmi` |
| Change test-mail recipient | `TEST_MAIL_TO=you@company.com` |
| Rows per page (hosts/alerts) | `PAGE_SIZE=300` |
| Hide/disable the CSV Export buttons | `ENABLE_EXPORT=false` |
| Show the Topology views (network map + service map, table + graph) | `ENABLE_TOPOLOGY=true` |

Restart the app after editing `.env`.

---

## 7. Operate from the UI / API

- **Refresh now** button (top right) — polls every instance immediately.
- **Send test mail** button (Zabbix instance cards) — sends a test email via
  that instance's SMTP relay.
- **Export CSV** buttons (Hosts, Alerts pages) — download the current, filtered view.
- **Shared** page — devices monitored by more than one instance (by IP).

JSON API (also at `/docs`):

```
GET  /api/v1/hosts?platform=&instance=&status=&q=
GET  /api/v1/alerts?active=true&q=
GET  /api/v1/hosts.csv           GET /api/v1/alerts.csv     # exports
GET  /api/v1/summary
GET  /api/v1/collectors/status
POST /api/v1/collectors/run                      # run all
POST /api/v1/collectors/{instance}/run           # run one
POST /api/v1/collectors/{instance}/test-mail?to= # Zabbix test mail
```

Quick checks from PowerShell:

```powershell
curl http://127.0.0.1:8000/api/v1/summary
curl -X POST http://127.0.0.1:8000/api/v1/collectors/run
```

---

## 8. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `polling 0 instance(s)` | `servers.yaml` missing or has a YAML error — check indentation (2/4 spaces, no tabs). |
| UI shows no data after going live | Stop, `Remove-Item dashboard.db`, restart (a prior MOCK run left stale rows). |
| An instance dot is **red** | Hover it for the exact error. Common: wrong URL/creds, or NNMi account missing the "Web Service Clients" role. |
| Zabbix instance 404 | The frontend is under a subpath; the app auto-tries `/zabbix`. If still failing, set the full path in `servers.yaml`, e.g. `.../zabbix`. |
| Design looks broken / unstyled | Hard refresh once: **Ctrl + F5**. |
| Slow with lots of data | Tables cap at 500 rows — use search/filters to narrow, or Export CSV for the full set. Raise `POLL_INTERVAL_MINUTES` to reduce load. |
| `pydantic-core` tries to build with Rust | You're on Python 3.13/3.14 — use **3.12** (step 0). |

---

## 9. Deploy to a server later

Switch the database and run under a service manager. See
[`README.md`](README.md#deploy-to-a-server-later) and [`ARCHITECTURE.md`](ARCHITECTURE.md)
for the PostgreSQL URL and the systemd unit. Run a **single Uvicorn worker** so
the in-process scheduler doesn't poll in parallel.
