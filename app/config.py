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

    # --- Zabbix -------------------------------------------------------------
    zabbix_url: str = ""
    zabbix_token: str = ""

    # --- Dynatrace ----------------------------------------------------------
    dynatrace_url: str = ""
    dynatrace_token: str = ""

    # --- NNMi ---------------------------------------------------------------
    nnmi_url: str = ""
    nnmi_user: str = ""
    nnmi_pass: str = ""

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
