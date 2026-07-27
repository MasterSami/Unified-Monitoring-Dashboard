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

    # Rows per page in the hosts / alerts tables.
    page_size: int = 300

    # Feature flag for the (in-progress) Topology / Service-Map / App-Map views.
    # Kept OFF: the nav entry is hidden and the page is a disabled placeholder
    # until the topology collectors are built. See TOPOLOGY.md.
    enable_topology: bool = False

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
