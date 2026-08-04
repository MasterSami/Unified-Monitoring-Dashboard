# SiteScope → UMD forwarder (production install)

Pushes redacted SiteScope events from the **SiteScope server** to the UMD
`/api/v1/ingest/sitescope` endpoint. PowerShell 5.1, no dependencies, no Python.

> **This runs on a critical production server. Read the safety section first.**

## Safety guarantees (by design)

- **Read-only** on the log — opened with `FileShare.ReadWrite`, so the SiteScope
  service and the Netcool probe keep writing normally. The forwarder **never**
  locks, writes, deletes, rotates, or truncates the log or any existing file.
- The **only** paths it writes are under `state_dir` (`D:\Scripts\umd\`):
  `.state.json` (checkpoint) and `forwarder.log` (its own capped log).
- **Never** touches the Netcool probe or the SiteScope service.
- **Redaction happens on this box** before anything is sent; `forwarder.log`
  never contains raw log lines or unredacted URLs.
- **Fail-safe checkpoint**: on any send failure the checkpoint is *not* advanced
  — nothing is lost, it retries next cycle. `event_id` makes re-sends idempotent
  (no duplicates in the UMD).
- Reads only the **new bytes** since the last checkpoint (light CPU/IO).

## Files

| File | Purpose |
| --- | --- |
| `sitescope_forwarder.ps1` | the forwarder |
| `config.example.json` | copy to `config.json` and edit |

Put both under `D:\Scripts\umd\` on the SiteScope server.

## Step 1 — configure

Copy `config.example.json` → `config.json` and set:
- `log_path` — the SiteScope OM-integration log.
- `umd_url` — the UMD ingest URL (later a reachable host; localhost only works
  when the forwarder and UMD are on the same machine).
- `source_instance` — a label for this SiteScope (e.g. `SiteScope-141`).

The **token is never in config**. Provide it one of two ways:
- **Env var** (simplest): set `SITESCOPE_INGEST_TOKEN` for the account that runs
  the task, or
- **DPAPI file** — create it *as the account that will run the task*:
  ```powershell
  "PASTE_THE_TOKEN" | ConvertTo-SecureString -AsPlainText -Force |
    ConvertFrom-SecureString | Set-Content D:\Scripts\umd\token.dpapi
  ```
  then set `"token_file": "D:\\Scripts\\umd\\token.dpapi"` in `config.json`.
  DPAPI ties the file to that account+machine; it can't be read elsewhere.

## Step 2 — validate with a DRY RUN (sends nothing, read-only)

```powershell
powershell -ExecutionPolicy Bypass -File D:\Scripts\umd\sitescope_forwarder.ps1 -DryRun
```
It reports how many new lines it *would* send and how many were redacted, and
prints one **redacted** sample line. It does **not** send and does **not** move
the checkpoint. Run it a couple of times — safe to repeat.

## Step 3 — one real cycle (manual)

```powershell
powershell -ExecutionPolicy Bypass -File D:\Scripts\umd\sitescope_forwarder.ps1
```
Check the UMD dashboard for SiteScope hosts + alerts, and `forwarder.log` for a
`sent N line(s)` entry. Re-running is safe (idempotent).

## Step 4 — dedicated low-privilege account + read-only NTFS

Create/choose a low-privilege service account (e.g. `svc_umd_fwd`) and grant it:
- **Read & Execute** on `D:\SiteScope\logs` (read-only — no write/delete).
- **Modify** on `D:\Scripts\umd` (its own state + log).
Example (adjust the account name):
```powershell
icacls "D:\SiteScope\logs" /grant "svc_umd_fwd:(OI)(CI)RX"
icacls "D:\Scripts\umd"    /grant "svc_umd_fwd:(OI)(CI)M"
```

## Step 5 — schedule (Task Scheduler, every 60s)

Run the single-cycle mode every minute under the service account:
```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NonInteractive -ExecutionPolicy Bypass -File D:\Scripts\umd\sitescope_forwarder.ps1'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Seconds 60) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName 'UMD-SiteScope-Forwarder' -Action $action -Trigger $trigger `
  -User 'svc_umd_fwd' -RunLevel Limited
```
(Alternatively run it with `-Loop` under NSSM as a service — same script.)

## Rollback — remove all traces from the SiteScope server

```powershell
Unregister-ScheduledTask -TaskName 'UMD-SiteScope-Forwarder' -Confirm:$false   # if scheduled
Remove-Item -Recurse -Force D:\Scripts\umd                                     # script, config, state, log, token
# optionally revoke the NTFS grants:
icacls "D:\SiteScope\logs" /remove "svc_umd_fwd"
```
Nothing else on the server was modified — the SiteScope log, service, and the
Netcool probe were never touched.
