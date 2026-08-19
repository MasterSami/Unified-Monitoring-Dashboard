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

    # How far back (days) collectors backfill RESOLVED alerts from the source
    # tools (Zabbix event history, Dynatrace closed problems) so the Alerts
    # "Resolved" view is real history, not just what resolved since deploy.
    alert_history_days: int = 30

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
