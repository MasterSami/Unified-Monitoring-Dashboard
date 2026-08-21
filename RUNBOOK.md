# Runbook — the team's script library

## Why it exists

When an awkward request comes in — *"which of these 40 IPs is actually
monitored?"*, *"give me every disabled host before the migration"* — someone on
the team writes a script. It does the job, it gets emailed around, and six
months later nobody remembers it exists. So it gets written again.

The Runbook is where those scripts live instead. Every one is documented in
plain language, findable by platform, and runnable by any admin from the browser
without a shell, a Python environment, or a copy of the credentials.

## Turning it on

Two environment variables, then restart:

```env
ENABLE_RUNBOOK=true
RUNBOOK_USERS=ahmed=<sha256-of-password>
```

Generate the second line — it never contains a readable password:

```bash
python -m app.runbook_auth hash
```

That prompts for a username and password and prints the exact `RUNBOOK_USERS=`
line to paste. Add more admins by appending `;user=digest` pairs.

Optional settings:

| Variable | Default | What it does |
|---|---|---|
| `RUNBOOK_SECRET` | random per process | HMAC key signing the session cookie. Set it to keep sessions alive across restarts. |
| `RUNBOOK_SESSION_MINUTES` | `60` | How long a sign-in lasts. |
| `RUNBOOK_MAX_ROWS` | `20000` | Hard cap on rows one script may return. |
| `RUNBOOK_USER` / `RUNBOOK_PASSWORD` | empty | Single-admin plaintext fallback. Prefer `RUNBOOK_USERS`. |

## Using it

Open **Runbook** in the sidebar and sign in. You get the catalogue, filterable by
platform. Click a script to see what it does step by step, which columns it
returns and which API methods it calls. Pick an instance, fill in any inputs,
press **Run script** — results appear in a table below, and **Export Excel**
downloads them in the standard SAMI'X template with the script author's credit
in the header.

## The two safety properties

**Read-only is enforced by the transport, not by discipline.** Every query a
script makes goes through `ZabbixCollector.read_rpc`, which raises if the method
is not a `*.get`. A future script cannot mutate Zabbix by accident, however it is
written.

**Write scripts are catalogued but never runnable from the web.** Three of the
original scripts genuinely change things — creating hosts, creating users, and
disabling monitoring. They appear in the Runbook with their full documentation,
marked *changes config*, and have no Run button. Bulk-disabling monitoring from
a dashboard button is exactly the kind of mistake that is discovered a week
later, when nobody noticed the alerts had stopped. Run those from the CLI, where
the target list is explicit and reviewable. `RUNBOOK_ALLOW_WRITE` exists in the
config for completeness and should stay `false`.

## What's in the catalogue

All Zabbix, for now.

| Script | What it answers |
|---|---|
| **Unavailable Hosts** | Which enabled hosts can Zabbix not currently reach? |
| **Disabled Hosts** | What has monitoring switched off, and what was it linked to? |
| **Host Inventory Backup** | Full snapshot of every host — take it before a bulk change. |
| **IP Lookup** | Given an IP, which Zabbix server monitors it, and as what? |
| **IP Monitoring Status** | For a list of IPs: present, enabled, and actually collecting? |
| **Proxy Status** | Proxy fleet health, plus how many hosts sit behind each one. |
| **Host Group Audit (vCenter)** | Which hosts in a group are enabled but collecting nothing? |
| **IP Metric History** | Raw numeric history for one IP over a chosen window. |
| *Bulk Add Hosts* | *(documented, CLI only — creates hosts)* |
| *Bulk Add Users* | *(documented, CLI only — creates users)* |
| *Bulk Disable Hosts* | *(documented, CLI only — stops monitoring)* |

## How the scripts are implemented

They are re-implemented as functions in `app/runbook.py`, not shelled out to the
original `.py` files. That was a deliberate choice:

- **Credentials stay in one place.** The originals each hardcoded a Zabbix URL,
  username and password. Here there are none — a run borrows the collector the
  scheduler already keeps authenticated, which reads `servers.yaml`.
- **A run costs one round trip.** Shelling out would mean a fresh `user.login`
  on every click.
- **Several of them got faster.** Four originals downloaded the entire host list
  and matched the IP in Python; these filter server-side. The item counts that
  drove one `item.get` per host are now a single batched call. `history.get` is
  issued once per value type rather than once per item.
- **Two of them got more correct.** Zabbix 6.0 moved host availability onto the
  interface, so the old `filter: {available: "2"}` silently returned nothing on
  newer servers — Unavailable Hosts now reads the interface and works on every
  version. Zabbix 6.4 renamed the proxy fields, so Proxy Status handles both
  schemas in one run.

## Adding a script

1. Write a runner in `app/runbook.py`: `(collectors, params) -> list[list]`.
   Query through `collector.read_rpc(...)`.
2. Register a `Script(...)` in `SCRIPTS` with its `purpose`, `steps`, `columns`
   and `api_calls`. The UI and the export are both generated from that entry —
   there is no template to touch.
3. Add a test in `tests/test_runbook.py`. `FakeZabbix` replays canned API
   responses, so runner tests need no network.

The catalogue-integrity test enforces that every read-only script has a runner,
that slugs are unique, and that each one carries real documentation.
