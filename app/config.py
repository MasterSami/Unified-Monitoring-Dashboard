"""Application configuration via pydantic-settings.

All runtime configuration is read from environment variables (optionally
sourced from a local ``.env`` file). No credentials are ever hardcoded.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core ---------------------------------------------------------------
    database_url: str = "sqlite:///./dashboard.db"
    poll_interval_minutes: int = 5
    enabled_collectors: str = "zabbix,dynatrace,nnmi"
    mock_mode: bool = True
    tls_verify: bool = False

    # Path to the YAML file listing all monitored servers (one entry per
    # instance, grouped by platform). See servers.example.yaml.
    servers_config: str = "servers.yaml"

    # Default recipient for the Zabbix "send test mail" action.
    test_mail_to: str = ""

    # Optional CC added to every alert "Escalate" mail (comma-separated).
    escalation_cc: str = ""

    # Rows per page in the hosts / alerts tables.
    page_size: int = 300

    # --- Overview presentation ----------------------------------------------
    # The "Hosts by status" strip on the Overview. Hidden by default: it repeats
    # numbers the Agent Health section already carries, and the large "unknown"
    # and "disabled" counts read as failures to anyone who does not know that
    # both are normal states. Set to true to bring it back.
    show_host_status_row: bool = False

    # Which alerts the "Critical Alerts" tile counts, matched against each
    # tool's OWN severity label (case-insensitive, comma-separated). Counting
    # the unified 1-5 scale swept in every platform's top tier and produced a
    # number too large to act on; this counts only what each tool itself calls
    # its most severe. Leave empty to fall back to unified severity 5.
    critical_alert_labels: str = "disaster,critical"

    @property
    def critical_alert_label_set(self) -> set[str]:
        """Normalized set of native severity labels counted as critical."""
        return {
            part.strip().lower()
            for part in self.critical_alert_labels.split(",")
            if part.strip()
        }

    # Re-stat every template on every render so edits show without a restart.
    # Off by default: the sidebar health strip re-renders on a 60s timer on
    # every open tab, and each render stats the page template plus every
    # {% extends %} / {% include %} it pulls in. Running under `uvicorn
    # --reload` restarts the process on a template edit anyway; turn this on
    # only if you edit templates against a server you are not restarting.
    template_auto_reload: bool = False

    # Cap on rows the raw JSON endpoints (/api/v1/hosts, /api/v1/alerts) return
    # when no explicit limit is given, so a bare GET can never serialize the
    # whole alert history.
    api_default_limit: int = 500
    api_max_limit: int = 5000

    # How far back (days) collectors backfill RESOLVED alerts from the source
    # tools (Zabbix event history, Dynatrace closed problems) so the Alerts
    # "Resolved" view is real history, not just what resolved since deploy.
    alert_history_days: int = 30

    # Minimum minutes between resolved-alert history backfills per instance.
    # History changes slowly, so backfilling every poll cycle is wasteful and
    # write-heavy; default once per hour. Set 0 to run every cycle.
    alert_history_refresh_minutes: int = 60

    # Show the CSV "Export" buttons (and enable the .csv endpoints).
    # Set to false to hide/disable export across the whole UI and API.
    enable_export: bool = True

    # Feature flag for the Topology views (NNMi network map + Dynatrace unified
    # service/app map). Kept OFF by default; enable it yourself. See TOPOLOGY.md.
    enable_topology: bool = False

    # Show the Topology export buttons (NNMi L2 CSV, Dynatrace map XLSX) and
    # enable the topology export endpoints. Separate switch so you can turn the
    # exports on independently. Enable it yourself from .env.
    enable_topology_export: bool = False

    # --- Runbook (admin script library) -------------------------------------
    # Feature flag for the Runbook tab — the team's library of operational
    # scripts, browsable and runnable from the UI. OFF by default; enable it
    # from .env exactly like ENABLE_TOPOLOGY. See RUNBOOK.md.
    enable_runbook: bool = False

    # Who may open the Runbook. Preferred form is a ';'-separated list of
    # "user=<sha256-hex-of-password>" entries, so no plaintext password is ever
    # stored:
    #   RUNBOOK_USERS=ahmed=9f86d0...;sami=6b86b2...
    # Generate a digest with:  python -m app.runbook_auth hash
    runbook_users: str = ""
    # Single-admin convenience fallback (plaintext, compared in constant time).
    # Prefer RUNBOOK_USERS in anything shared.
    runbook_user: str = ""
    runbook_password: str = ""

    # HMAC key signing the Runbook session cookie. Leave empty and a random key
    # is generated per process (sessions then end on restart, which is fine for
    # a single instance). Set it to keep sessions valid across restarts.
    runbook_secret: str = ""
    # How long a Runbook login stays valid.
    runbook_session_minutes: int = 60

    # Hard cap on rows a single Runbook script may return, so one broad query
    # can never exhaust memory or freeze the browser.
    runbook_max_rows: int = 20000

    # Scripts that WRITE to the monitoring tools (create hosts/users, disable
    # monitoring) are documented in the Runbook but never runnable from the web
    # UI while this is false. Leave it off unless you have a strong reason.
    runbook_allow_write: bool = False

    # --- SiteScope push ingest ---------------------------------------------
    # Bearer token the SiteScope forwarder must present to POST events. Empty
    # (default) disables the ingest endpoint entirely (returns 503). Set it in
    # the environment, never in code.
    sitescope_ingest_token: str = ""
    # Hard caps on an ingest request (matches the forwarder's batch size of 500).
    ingest_max_events: int = 500
    ingest_max_bytes: int = 5_000_000

    # --- SiteScope LOCAL auto-load (env-only, no code changes needed) -------
    # Point UMD at REDACTED SiteScope .tsv file(s) on disk and it auto-loads them
    # on startup and re-reads them every poll interval — SiteScope shows up as a
    # full platform (hosts + alerts). If the file is kept fresh (a scheduled
    # redact-export on the SiteScope box regenerating it), the data stays LIVE.
    #
    # Single file (legacy):
    sitescope_demo_file: str = ""
    sitescope_demo_instance: str = "SiteScope-141"
    #
    # Multiple SiteScope servers at once — a ';'-separated list of
    # "Instance=Path" entries (Windows paths keep their drive colon):
    #   SITESCOPE_DEMO_FILES=SiteScope-141=D:\umd\sis141.tsv;SiteScope-140=D:\umd\sis140.tsv;SiteScope-34=D:\umd\sis34.tsv
    sitescope_demo_files: str = ""

    @property
    def sitescope_demo_map(self) -> list[tuple[str, str]]:
        """Parse the demo config into ``[(instance, path), ...]`` (deduped)."""
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(instance: str, path: str) -> None:
            instance, path = instance.strip(), path.strip()
            key = instance or path
            if path and key not in seen:
                seen.add(key)
                pairs.append((instance or "SiteScope", path))

        if self.sitescope_demo_file:
            _add(self.sitescope_demo_instance or "SiteScope-141", self.sitescope_demo_file)
        for entry in self.sitescope_demo_files.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            instance, sep, path = entry.partition("=")
            _add(instance, path) if sep else _add("SiteScope", instance)
        return pairs

    @property
    def enabled_collectors_list(self) -> list[str]:
        """Return the enabled collector names as a normalized list."""
        return [
            name.strip().lower()
            for name in self.enabled_collectors.split(",")
            if name.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
