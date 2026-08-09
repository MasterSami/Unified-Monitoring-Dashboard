"""Diagnose which NNMi node fields carry the management IP.

The collector maps a host's IP from a set of candidate node fields. When the IP
doesn't show, this read-only probe fetches a page of nodes from each NNMi
instance and prints every field name it sees (with a sample value), flagging the
ones that look like IP addresses. Paste the output back and the exact field can
be mapped precisely.

Usage:
    python tools/nnmi_probe_fields.py                 # all NNMi instances
    python tools/nnmi_probe_fields.py --instance NNMi-DC1
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.nnmi import (  # noqa: E402
    _IPADDR_NS,
    _NODE_NS,
    _SOAP_TEMPLATE,
    NnmiCollector,
)
from app.config import get_settings  # noqa: E402
from app.servers import load_servers  # noqa: E402

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _sample(collector: NnmiCollector, path: str, ns: str, operation: str) -> list[dict]:
    """Fetch one small page (read-only), trying each filter; skip ones that error.

    Some beans (e.g. IPAddressBean) have no ``name`` attribute and 500 on the
    name filter, so failures per-condition are swallowed and the next is tried.
    """
    last_exc: Exception | None = None
    for field, op, value in (("id", "GE", "0"), ("name", "LIKE", "%")):
        envelope = _SOAP_TEMPLATE.format(
            ns=ns, operation=operation, offset=0, max_objects=50,
            cond_field=field, cond_op=op, cond_value=value,
        )
        try:
            rows = collector._parse_items(collector._soap_post(path, envelope))
        except Exception as exc:  # noqa: BLE001 — diagnostic; try next filter
            last_exc = exc
            continue
        if rows:
            return rows
    if last_exc is not None:
        raise last_exc
    return []


def _dump_fields(records: list[dict], entity: str) -> None:
    print(f"\n{entity} sampled: {len(records)}")
    if not records:
        print("  (none returned)")
        return
    fields: dict[str, dict] = defaultdict(lambda: {"count": 0, "sample": "", "iplike": 0})
    for r in records:
        for k, v in r.items():
            f = fields[k]
            f["count"] += 1
            if v and not f["sample"]:
                f["sample"] = v
            if v and _IP_RE.search(str(v)):
                f["iplike"] += 1
    print(f"{'FIELD':26} {'ROWS':>6} {'IP?':>4}  SAMPLE")
    for k in sorted(fields, key=lambda k: (-fields[k]["iplike"], k)):
        f = fields[k]
        flag = "IP" if f["iplike"] else ""
        print(f"{k:26} {f['count']:>6} {flag:>4}  {f['sample'][:48]}")


def probe(collector: NnmiCollector) -> None:
    print(f"\n=== {collector.instance} ===")
    nodes = _sample(collector, "/NodeBeanService/NodeBean", _NODE_NS, "getNodes")
    _dump_fields(nodes, "NODES")

    # IPAddressBean is where IPs live for nodes that carry no address on the
    # node record. Sample it too so the node-id <-> ip join fields are visible.
    try:
        ips = _sample(
            collector, "/IPAddressBeanService/IPAddressBean", _IPADDR_NS, "getIPAddresses"
        )
        _dump_fields(ips, "IP ADDRESSES")
    except Exception as exc:  # noqa: BLE001 — diagnostic
        print(f"\nIP ADDRESSES sample failed: {exc}")
    print("\n-> node IP = a node field flagged 'IP', or IPAddressBean value joined by node id.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default=None, help="only this NNMi instance")
    args = ap.parse_args()

    settings = get_settings()
    if settings.mock_mode:
        print("MOCK_MODE=true — set it to false to probe the real NNMi.", file=sys.stderr)
        return 2

    ran = 0
    for cfg in load_servers(settings):
        if cfg.platform != "nnmi":
            continue
        if args.instance and cfg.name != args.instance:
            continue
        try:
            probe(NnmiCollector(cfg, settings))
            ran += 1
        except Exception as exc:  # noqa: BLE001 — diagnostic, keep going
            print(f"\n=== {cfg.name} === FAILED: {exc}", file=sys.stderr)
    if ran == 0:
        print("no matching NNMi instance found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
