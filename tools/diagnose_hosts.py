#!/usr/bin/env python3
"""Explain the "Total Active Servers" number against your own database.

The tile counts DISTINCT devices, keyed on IP and falling back to hostname when
the tool recorded no usable IP. When that number looks far too small, the cause
is almost always several hosts sharing one IP — a placeholder like 127.0.0.1, a
NAT address, or a management address reused across many nodes — so they collapse
into a single "device".

This prints the arithmetic and then names the keys doing the collapsing, so you
can see whether the number is wrong or the inventory is.

    .venv\\Scripts\\python.exe tools\\diagnose_hosts.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Host, HostStatus  # noqa: E402
from app.routers.pages import PLACEHOLDER_IPS, _device_key  # noqa: E402

BAR = "-" * 72


def main() -> None:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(
                Host.source_platform,
                Host.source_instance,
                Host.hostname,
                Host.ip,
                Host.status,
                Host.agent_deployed,
            )
        ).all()

        up = [r for r in rows if getattr(r.status, "value", r.status) == "up"]
        distinct = db.scalar(
            select(func.count(func.distinct(_device_key()))).where(
                Host.status == HostStatus.up
            )
        )

        print(BAR)
        print("HOST RECORDS")
        print(BAR)
        per_platform: Counter = Counter()
        per_status: Counter = Counter()
        for r in rows:
            per_platform[getattr(r.source_platform, "value", r.source_platform)] += 1
            per_status[getattr(r.status, "value", r.status)] += 1
        for name, count in per_platform.most_common():
            print(f"  {name:12} {count:>8}")
        print(f"  {'TOTAL':12} {len(rows):>8}")
        print()
        for name, count in per_status.most_common():
            print(f"  status {name:10} {count:>8}")

        print()
        print(BAR)
        print("THE TILE'S ARITHMETIC")
        print(BAR)
        print(f"  host records with status = up   : {len(up):>8}")
        print(f"  distinct devices after dedup    : {distinct:>8}")
        print(f"  collapsed by the dedup key      : {len(up) - (distinct or 0):>8}")

        # Which keys are absorbing many rows?
        groups: dict[str, list] = defaultdict(list)
        placeholders = 0
        for r in up:
            ip = (r.ip or "").strip()
            if ip.lower() in PLACEHOLDER_IPS:
                placeholders += 1
                key = (r.hostname or "").lower()
            else:
                key = ip or (r.hostname or "").lower()
            groups[key].append(r)

        shared = {k: v for k, v in groups.items() if len(v) > 1}
        cross = {k: v for k, v in shared.items()
                 if len({getattr(r.source_platform, "value", r.source_platform)
                         for r in v}) > 1}
        same = {k: v for k, v in shared.items() if k not in cross}

        print()
        print(f"  up rows on a placeholder IP {PLACEHOLDER_IPS}:")
        print(f"    {placeholders:>8}   (these key on hostname instead)")
        print(f"  keys shared by 2+ tools     : {len(cross):>8}   <- real duplicates")
        print(f"  keys shared inside ONE tool : {len(same):>8}   <- suspicious")

        if same:
            print()
            print(BAR)
            print("KEYS COLLAPSING ROWS FROM A SINGLE TOOL (top 15)")
            print("These are the ones that make the tile too small.")
            print(BAR)
            for key, members in sorted(same.items(), key=lambda kv: -len(kv[1]))[:15]:
                platform = getattr(
                    members[0].source_platform, "value", members[0].source_platform
                )
                names = ", ".join(m.hostname or "?" for m in members[:4])
                more = f" (+{len(members) - 4} more)" if len(members) > 4 else ""
                print(f"  {key:24} {len(members):>5} rows  [{platform}]  {names}{more}")

        if cross:
            print()
            print(BAR)
            print("DEVICES GENUINELY SEEN BY MORE THAN ONE TOOL (top 10)")
            print(BAR)
            for key, members in sorted(cross.items(), key=lambda kv: -len(kv[1]))[:10]:
                tools = ", ".join(sorted({
                    getattr(m.source_platform, "value", m.source_platform)
                    for m in members
                }))
                print(f"  {key:24} {len(members):>5} rows  [{tools}]")

        print()
        print(BAR)
        print("VERDICT")
        print(BAR)
        if len(same) > len(cross):
            print("  Most of the collapsing happens INSIDE a single tool, which means")
            print("  the IPs above are not device identities. Either fix them in the")
            print("  source tool, or add them to PLACEHOLDER_IPS in app/routers/pages.py.")
        elif len(up) - (distinct or 0) < len(up) * 0.1:
            print("  Dedup is barely doing anything; the tile reflects the inventory.")
        else:
            print("  The collapsing is genuine cross-tool overlap. The tile is right,")
            print("  and the gap is how much the tools duplicate each other.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
