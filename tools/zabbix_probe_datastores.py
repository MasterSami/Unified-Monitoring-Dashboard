"""Check whether your Zabbix instances already collect vCenter datastore sizes.

The official VMware template discovers datastores and creates items keyed
``vmware.datastore.size[<url>,<datastore>,...]``. If those items exist and carry
values, a datastore capacity view can be built with no change on the Zabbix
side — the data is already there.

This is read-only: it lists the keys, how many datastores each instance sees,
and a sample of the current values, so you can tell "we have this already" from
"the template is not linked" before anything is built on top of it.

Usage:
    python tools/zabbix_probe_datastores.py
    python tools/zabbix_probe_datastores.py --instance Zabbix-68
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.zabbix import ZabbixCollector  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.servers import load_servers  # noqa: E402

GB = 1024.0 ** 3
TB = 1024.0 ** 4

#: The datastore name is the second argument of the item key.
_NAME_RE = re.compile(r"vmware\.datastore\.\w+\[[^,]+,([^,\]]+)")


def _datastore_name(key: str) -> str:
    match = _NAME_RE.search(key)
    return match.group(1).strip() if match else "?"


def _mode(key: str) -> str:
    """Which figure this item carries: total, free, pfree, uncommitted…"""
    inside = key[key.find("[") + 1 : key.rfind("]")]
    parts = [p.strip() for p in inside.split(",")]
    return parts[2] if len(parts) > 2 else "total"


def _fmt_bytes(value: float) -> str:
    return f"{value / TB:.1f} TB" if value >= TB else f"{value / GB:.0f} GB"


def probe(collector: ZabbixCollector) -> None:
    print(f"\n=== {collector.instance} ===")
    try:
        items = collector.read_rpc(
            "item.get",
            {
                "output": ["itemid", "hostid", "name", "key_", "lastvalue", "units"],
                "search": {"key_": "vmware.datastore."},
                "searchWildcardsEnabled": False,
                "filter": {"status": "0"},
            },
        )
    except Exception as exc:  # noqa: BLE001 — one instance failing is fine
        print(f"  [FAIL] {exc}")
        return

    items = list(items or [])
    if not items:
        print("  no vmware.datastore.* items found")
        print("  -> the VMware template is probably not linked to the vCenter host,")
        print("     or datastore discovery has not run yet.")
        return

    by_key_shape: dict[str, int] = defaultdict(int)
    stores: dict[str, dict[str, str]] = defaultdict(dict)
    for it in items:
        key = it.get("key_", "")
        shape = key.split("[")[0]
        by_key_shape[f"{shape}[…,{_mode(key)}]"] += 1
        stores[_datastore_name(key)][_mode(key)] = it.get("lastvalue", "")

    print(f"  {len(items)} item(s) across {len(stores)} datastore(s)")
    print("\n  key shapes found:")
    for shape, count in sorted(by_key_shape.items(), key=lambda kv: -kv[1]):
        print(f"    {shape:52} {count:>5} item(s)")

    empty = sum(
        1 for modes in stores.values() if not any(v not in ("", None) for v in modes.values())
    )
    if empty:
        print(f"\n  {empty} datastore(s) have items but no values yet")

    print("\n  sample (first 10 datastores):")
    header = f"    {'datastore':32} {'total':>10} {'free %':>8} {'uncommitted':>12}"
    print(header)
    print("    " + "-" * (len(header) - 4))
    for name, modes in list(stores.items())[:10]:
        def num(mode: str) -> float | None:
            raw = modes.get(mode)
            try:
                return float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        total, pfree, uncommitted = num("total"), num("pfree"), num("uncommitted")
        print(
            f"    {name[:32]:32} "
            f"{(_fmt_bytes(total) if total else '-'):>10} "
            f"{(f'{pfree:.1f}%' if pfree is not None else '-'):>8} "
            f"{(_fmt_bytes(uncommitted) if uncommitted else '-'):>12}"
        )

    # The number that matters most and nobody watches: thin-provisioned space
    # promised beyond what the array actually holds.
    totals = []
    overcommit = []
    for modes in stores.values():
        try:
            totals.append(float(modes.get("total") or 0))
        except ValueError:
            pass
        try:
            overcommit.append(float(modes.get("uncommitted") or 0))
        except ValueError:
            pass
    if totals:
        print(f"\n  total capacity across datastores : {_fmt_bytes(sum(totals))}")
    if any(overcommit):
        print(f"  uncommitted (thin-provisioned)   : {_fmt_bytes(sum(overcommit))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", action="append", help="limit to these instances")
    args = ap.parse_args()

    settings = get_settings()
    if settings.mock_mode:
        sys.exit("MOCK_MODE is on — set MOCK_MODE=false to probe real servers.")

    wanted = {i.lower() for i in (args.instance or [])}
    servers = [
        s for s in load_servers(settings)
        if s.platform == "zabbix" and (not wanted or s.name.lower() in wanted)
    ]
    if not servers:
        sys.exit("no matching Zabbix instance in servers.yaml")

    for cfg in servers:
        probe(ZabbixCollector(cfg, settings))


if __name__ == "__main__":
    main()
