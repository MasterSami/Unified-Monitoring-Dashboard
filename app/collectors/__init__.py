"""Collector registry and factory (one collector per configured server)."""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.collectors.dynatrace import DynatraceCollector
from app.collectors.nnmi import NnmiCollector
from app.collectors.zabbix import ZabbixCollector
from app.config import Settings
from app.servers import ServerConfig, load_servers

#: Map of platform name -> concrete collector class.
COLLECTOR_CLASSES: dict[str, type[BaseCollector]] = {
    "zabbix": ZabbixCollector,
    "dynatrace": DynatraceCollector,
    "nnmi": NnmiCollector,
}


def build_collector(config: ServerConfig, settings: Settings) -> BaseCollector | None:
    """Instantiate the collector for a single server config."""
    cls = COLLECTOR_CLASSES.get(config.platform)
    return cls(config, settings) if cls else None


def build_collectors(settings: Settings) -> dict[str, BaseCollector]:
    """Instantiate one collector per configured server, keyed by instance name."""
    collectors: dict[str, BaseCollector] = {}
    for config in load_servers(settings):
        collector = build_collector(config, settings)
        if collector is not None:
            collectors[config.name] = collector
    return collectors


__all__ = [
    "BaseCollector",
    "COLLECTOR_CLASSES",
    "build_collector",
    "build_collectors",
]
