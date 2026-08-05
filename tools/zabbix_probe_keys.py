"""Diagnose which CPU / memory / disk item keys your Zabbix hosts actually use.

The Capacity view derives CPU%, memory and disk from a set of standard item
keys. When some hosts show a metric and others don't, it's because those hosts
use a DIFFERENT key (another template, SNMP, a custom item). This read-only
probe connects with your existing collector config (servers.yaml / .env) and
prints, per Zabbix instance, every distinct ``vm.memory* / system.cpu* / vfs.fs*``
item key, how many hosts and items carry it, and how many have an empty value.

Paste the output back and the exact missing keys can be mapped precisely.

Usage:
    python tools/zabbix_probe_keys.py                # all Zabbix instances
    python tools/zabbix_probe_keys.py --instance Zabbix-34
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.zabbix import ZabbixCollector  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.servers import load_servers  # noqa: E402


def _is_number(v: object) -> bool:
    try:
        float(v)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def probe(collector: ZabbixCollector) -> None:
    items = collector._rpc(
        "item.get",
        {
            "output": ["hostid", "key_", "lastvalue"],
            "search": {"key_": ["system.cpu", "vm.memory", "vfs.fs"]},
            "searchByAny": True,
            "startSearch": True,
            "monitored": True,
        },
    )
    # key_ -> {"items": n, "hosts": set, "empty": n, "sample": lastvalue}
    stats: dict[str, dict] = defaultdict(
        lambda: {"items": 0, "hosts": set(), "empty": 0, "sample": ""}
    )
    hosts_with_mem: set = set()
    all_hosts: set = set()
    for it in items:  # type: ignore[union-attr]
        key = it.get("key_", "")
        hid = str(it.get("hostid"))
        lv = it.get("lastvalue")
        all_hosts.add(hid)
        s = stats[key]
        s["items"] += 1
        s["hosts"].add(hid)
        if _is_number(lv):
            if not s["sample"]:
                s["sample"] = str(lv)
        else:
            s["empty"] += 1
        if key.startswith("vm.memory"):
            hosts_with_mem.add(hid)

    print(f"\n=== {collector.instance} ===")
    print(f"hosts with any cpu/mem/disk item: {len(all_hosts)}")
    print(f"hosts with a vm.memory* item:      {len(hosts_with_mem)}")
    missing = len(all_hosts) - len(hosts_with_mem)
    if missing:
        print(f"  -> {missing} host(s) have NO vm.memory* key (memory will be '—')")
    print(f"{'KEY':52} {'ITEMS':>6} {'HOSTS':>6} {'EMPTY':>6}  SAMPLE")
    for key in sorted(stats):
        s = stats[key]
        print(f"{key:52} {s['items']:>6} {len(s['hosts']):>6} {s['empty']:>6}  {s['sample']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default=None, help="only this Zabbix instance")
    args = ap.parse_args()

    settings = get_settings()
    if settings.mock_mode:
        print("MOCK_MODE=true — set it to false to probe the real Zabbix.", file=sys.stderr)
        return 2

    ran = 0
    for cfg in load_servers(settings):
        if cfg.platform != "zabbix":
            continue
        if args.instance and cfg.name != args.instance:
            continue
        try:
            probe(ZabbixCollector(cfg, settings))
            ran += 1
        except Exception as exc:  # noqa: BLE001 — diagnostic, keep going
            print(f"\n=== {cfg.name} === FAILED: {exc}", file=sys.stderr)
    if ran == 0:
        print("no matching Zabbix instance found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
