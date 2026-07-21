"""Collector registry and factory."""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.collectors.dynatrace import DynatraceCollector
from app.collectors.nnmi import NnmiCollector
from app.collectors.zabbix import ZabbixCollector
from app.config import Settings

#: Map of collector name -> concrete collector class.
COLLECTOR_CLASSES: dict[str, type[BaseCollector]] = {
    "zabbix": ZabbixCollector,
    "dynatrace": DynatraceCollector,
    "nnmi": NnmiCollector,
}


def build_collectors(settings: Settings) -> dict[str, BaseCollector]:
    """Instantiate the enabled collectors keyed by name."""
    return {
        name: COLLECTOR_CLASSES[name](settings)
        for name in settings.enabled_collectors_list
        if name in COLLECTOR_CLASSES
    }


def build_collector(name: str, settings: Settings) -> BaseCollector | None:
    """Instantiate a single collector by name (regardless of enabled list)."""
    cls = COLLECTOR_CLASSES.get(name)
    return cls(settings) if cls else None


__all__ = [
    "BaseCollector",
    "COLLECTOR_CLASSES",
    "build_collectors",
    "build_collector",
]
