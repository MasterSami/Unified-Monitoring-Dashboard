"""Local loader / dry-run for SiteScope redacted sample files.

Reads an **already-redacted** tab-delimited export (produced on the SiteScope
box) and either:

* ``--dry-run`` — parses and prints normalized JSON, sending nothing; or
* posts it in batches to a running UMD ``/api/v1/ingest/sitescope``.

This lets you feed real (redacted) SiteScope data into a local UMD with no
network exposure of the SiteScope server. It reuses the canonical parser in
``app.sitescope`` and redacts again as a safety net.

Usage:
    python tools/sitescope_local_ingest.py --file sample.redacted.tsv --dry-run
    python tools/sitescope_local_ingest.py --file sample.redacted.tsv \
        --url http://127.0.0.1:8000/api/v1/ingest/sitescope \
        --token "$SITESCOPE_INGEST_TOKEN" --instance SIS-Cairo-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

# Make the app package importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sitescope import ParseError, parse_line, redact  # noqa: E402


def _read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return [ln.rstrip("\r\n") for ln in fh if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="redacted .tsv export path")
    ap.add_argument("--instance", default="SIS-local", help="source_instance label")
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/v1/ingest/sitescope")
    ap.add_argument("--token", default=os.environ.get("SITESCOPE_INGEST_TOKEN", ""))
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true", help="parse + print, send nothing")
    args = ap.parse_args()

    lines = _read_lines(args.file)
    print(f"read {len(lines)} lines from {args.file}", file=sys.stderr)

    if args.dry_run:
        ok = skipped = redactions = 0
        for ln in lines:
            _, fired = redact(ln)
            redactions += fired
            try:
                ev = parse_line(ln, args.instance)
            except ParseError as exc:
                skipped += 1
                print(f"# SKIP: {exc}", file=sys.stderr)
                continue
            ok += 1
            d = asdict(ev)
            d["started_at"] = d["started_at"].isoformat() if d["started_at"] else None
            print(json.dumps(d, ensure_ascii=False))
        print(
            f"\nparsed={ok} skipped={skipped} redactions_fired={redactions} (nothing sent)",
            file=sys.stderr,
        )
        return 0

    try:
        import httpx
    except ImportError:
        print("httpx is required to send (or use --dry-run)", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    totals = {"inserted": 0, "updated": 0, "skipped": 0, "redactions": 0}
    for i in range(0, len(lines), args.batch):
        chunk = lines[i : i + args.batch]
        resp = httpx.post(
            args.url,
            json={"source_instance": args.instance, "lines": chunk},
            headers=headers,
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1
        r = resp.json()
        for k in totals:
            totals[k] += r.get(k, 0)
        print(f"batch {i // args.batch + 1}: {r}", file=sys.stderr)
    print(f"\nTOTAL: {totals}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
