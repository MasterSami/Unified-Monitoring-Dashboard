"""Server inventory: one entry per monitored instance.

Multiple servers per platform are supported (e.g. several Zabbix instances).
Real servers are declared in a YAML file (``servers.yaml`` by default) that is
kept out of version control because it holds credentials. In ``MOCK_MODE`` a
built-in list of fake instances is used so the app runs with no config file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.config import Settings

logger = logging.getLogger("servers")

_PLATFORMS = ("zabbix", "dynatrace", "nnmi")


class ServerConfig(BaseModel):
    """Connection details and options for a single monitored instance."""

    name: str
    platform: str
    url: str = ""
    user: str = ""
    password: str = ""
    token: str = ""
    #: Zabbix only: also collect proxy health for this instance.
    check_proxies: bool = False
    #: Zabbix only: expose a "send test mail" action for this instance.
    test_mail: bool = False
    #: Per-instance TLS override; falls back to the global TLS_VERIFY.
    verify_tls: bool | None = None
    extra: dict = Field(default_factory=dict)


# Built-in fake inventory for MOCK_MODE (two Zabbix, one Dynatrace, two NNMi).
MOCK_SERVERS: list[ServerConfig] = [
    ServerConfig(name="Zabbix-DC1", platform="zabbix", url="mock", check_proxies=True, test_mail=True),
    ServerConfig(name="Zabbix-DC2", platform="zabbix", url="mock", check_proxies=True, test_mail=True),
    ServerConfig(name="Dynatrace-Prod", platform="dynatrace", url="mock"),
    ServerConfig(name="NNMi-Core", platform="nnmi", url="mock"),
    ServerConfig(name="NNMi-Edge", platform="nnmi", url="mock"),
]


def _dedupe_names(servers: list[ServerConfig]) -> list[ServerConfig]:
    """Ensure instance names are unique (they key everything downstream)."""
    seen: dict[str, int] = {}
    out: list[ServerConfig] = []
    for s in servers:
        if s.name in seen:
            seen[s.name] += 1
            s = s.model_copy(update={"name": f"{s.name}-{seen[s.name]}"})
            logger.warning("duplicate server name; renamed to %s", s.name)
        else:
            seen[s.name] = 1
        out.append(s)
    return out


def load_servers(settings: Settings) -> list[ServerConfig]:
    """Return the active server inventory, filtered by ENABLED_COLLECTORS.

    In mock mode, returns the built-in fake instances. Otherwise reads the YAML
    file at ``settings.servers_config``; a missing file yields an empty list
    with a clear warning (the app still starts, just with no data).

    The parse is cached against the file's mtime and size. This is called from
    the sidebar health strip, which every page renders and re-polls every 60s,
    so without the cache each open browser tab cost a disk read, a full YAML
    parse and a Pydantic revalidation per minute. Editing ``servers.yaml`` still
    takes effect on the next call — the mtime changes and the cache misses.
    """
    enabled = set(settings.enabled_collectors_list)

    if settings.mock_mode:
        servers = list(MOCK_SERVERS)
    else:
        servers = _load_yaml_cached(settings.servers_config)

    servers = [s for s in servers if s.platform in enabled]
    return _dedupe_names(servers)


#: path -> (mtime, size, parsed). Keyed on both so a same-second rewrite of a
#: different length is still picked up.
_yaml_cache: dict[str, tuple[float, int, list[ServerConfig]]] = {}


def _load_yaml_cached(path: str) -> list[ServerConfig]:
    """:func:`_load_yaml`, memoized on the file's (mtime, size)."""
    try:
        stat = Path(path).stat()
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        stamp = (0.0, -1)  # missing file: _load_yaml warns, cache the empty list

    cached = _yaml_cache.get(path)
    if cached is not None and (cached[0], cached[1]) == stamp:
        return cached[2]

    servers = _load_yaml(path)
    _yaml_cache[path] = (stamp[0], stamp[1], servers)
    return servers


def reset_server_cache() -> None:
    """Drop the parsed-YAML cache (tests, and after an on-disk edit)."""
    _yaml_cache.clear()


def _load_yaml(path: str) -> list[ServerConfig]:
    """Parse the servers YAML file into a flat list of ServerConfig."""
    p = Path(path)
    if not p.exists():
        logger.warning(
            "servers config %s not found; no live servers configured. "
            "Copy servers.example.yaml to %s and fill it in.",
            path,
            path,
        )
        return []

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("failed to parse %s: %s", path, exc)
        return []

    servers: list[ServerConfig] = []
    # Accept either {zabbix: [...], dynatrace: [...]} or {servers: [...]}.
    if isinstance(raw, dict) and any(k in raw for k in _PLATFORMS):
        for platform in _PLATFORMS:
            for entry in raw.get(platform, []) or []:
                servers.append(_coerce(entry, platform))
    else:
        for entry in raw.get("servers", []) if isinstance(raw, dict) else []:
            platform = str(entry.get("platform", "")).lower()
            if platform in _PLATFORMS:
                servers.append(_coerce(entry, platform))

    logger.info("loaded %d server(s) from %s", len(servers), path)
    return servers


def _coerce(entry: dict, platform: str) -> ServerConfig:
    """Build a ServerConfig from a raw YAML entry, tolerating alias keys."""
    data = dict(entry)
    data["platform"] = platform
    # Common alias keys.
    if "pass" in data and "password" not in data:
        data["password"] = data.pop("pass")
    known = set(ServerConfig.model_fields) - {"extra"}
    extra = {k: v for k, v in data.items() if k not in known}
    clean = {k: v for k, v in data.items() if k in known}
    clean["extra"] = extra
    return ServerConfig(**clean)
