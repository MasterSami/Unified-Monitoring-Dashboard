"""Diagnose a host Dynatrace shows but SAMI'X does not.

    python -m app.dynatrace_probe --ip 10.22.68.11 10.22.68.12
    python -m app.dynatrace_probe --ip-range 10.22.68.11-10.22.68.20

For each address, queries every configured Dynatrace instance twice: once the
way the collector used to (no explicit time window, so Dynatrace's own short
default applies), and once with a wide window. A host that only the wide call
finds confirms the collector was excluding it by age, not by anything wrong
with the host itself — the collector now sends the wide window by default
(``DYNATRACE_ENTITY_LOOKBACK_DAYS``), so this reproduces what an *unpatched*
build would have missed and confirms a patched one no longer does.

A host absent from both calls is not a time-window problem: check the API
token's scope and, on a tenant using management-zone-restricted access
policies, whether that policy actually grants this zone. Also prints whether
each address is a HOST entity's primary IP or a secondary one, and whether it
is already in the local database under a different address on the same host —
see Host.ip_all.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Host, SourcePlatform

logger = logging.getLogger("dynatrace.probe")

#: A window wide enough to answer "has Dynatrace ever reported this host",
#: distinct from the collector's own (already wide) day-to-day setting — this
#: is a one-off diagnostic, not something run every poll.
_WIDE_DAYS = 730


def _expand_ip_range(spec: str) -> list[str]:
    """``"10.22.68.11-10.22.68.20"`` -> every address in between, inclusive.

    Both ends must be valid IPv4 addresses in the same /24 direction (the
    range increases); anything else is rejected with a plain message rather
    than silently doing something surprising.
    """
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return [spec.strip()]
    start, end = ipaddress.IPv4Address(start_s.strip()), ipaddress.IPv4Address(end_s.strip())
    if int(end) < int(start):
        raise ValueError(f"range end {end} is before start {start}")
    span = int(end) - int(start) + 1
    if span > 512:
        raise ValueError(f"range spans {span} addresses; keep a probe run under 512")
    return [str(ipaddress.IPv4Address(int(start) + i)) for i in range(span)]


def _query_entities(collector, *, ip: str, from_days: int | None) -> list[dict]:
    """One ``/api/v2/entities`` call, filtered to a single IP. Best-effort.

    Returns the raw entity list (usually 0 or 1 items — a NAT'd or shared IP
    could in principle match more than one host). Raises on a transport or API
    error rather than swallowing it: the caller wants to see a 4xx here, it is
    diagnostic in itself (an expired token, a scope the API token lacks).
    """
    url = f"{collector._base}/api/v2/entities"
    params: dict[str, str] = {
        "entitySelector": f'type("HOST"),ipAddress("{ip}")',
        "fields": "properties,managementZones",
        "pageSize": "50",
    }
    if from_days is not None:
        params["from"] = f"now-{from_days}d"
    with collector._client(headers=collector._headers()) as client:
        resp = collector._request_with_retries(client, "GET", url, params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("entities", [])


def _describe(entity: dict, ip: str) -> None:
    props = entity.get("properties", {})
    mode = props.get("monitoringMode") or "(not reported)"
    state = props.get("state") or "(not reported)"
    ips = props.get("ipAddress")
    ips = ips if isinstance(ips, list) else ([ips] if ips else [])
    zones = [z.get("name") for z in entity.get("managementZones") or [] if z.get("name")]

    print(f"    entityId        : {entity.get('entityId')}")
    print(f"    displayName     : {entity.get('displayName')}")
    print(f"    monitoringMode  : {mode}")
    print(f"    state           : {state}")
    print(f"    all IPs         : {', '.join(ips) if ips else '(none reported)'}")
    if len(ips) > 1:
        position = "primary (1st)" if ips[0] == ip else f"secondary (#{ips.index(ip) + 1})"
        print(f"                      -> {ip} is the {position} address on this host")
    print(f"    management zones: {', '.join(zones) if zones else '(none)'}")


def probe(db: Session, ips: list[str]) -> None:
    """Run the two-window comparison for each IP against every Dynatrace instance."""
    from app.scheduler import get_service

    settings = get_settings()
    dynatrace = {
        name: c for name, c in get_service().collectors.items()
        if getattr(c, "name", "") == "dynatrace"
    }
    if not dynatrace:
        print("No Dynatrace instance is configured (check ENABLED_COLLECTORS "
              "and servers.yaml).")
        return

    for ip in ips:
        print(f"\n=== {ip} ===")

        local = db.scalars(
            select(Host).where(
                Host.source_platform == SourcePlatform.dynatrace,
                (Host.ip == ip) | (Host.ip_all.ilike(f"%{ip}%")),
            )
        ).all()
        if local:
            for h in local:
                which = "primary" if h.ip == ip else "secondary (ip_all)"
                print(f"  already in SAMI'X: {h.hostname} on {h.source_instance} "
                      f"({which} address)")
        else:
            print("  not in SAMI'X's database under this address.")

        found_anywhere = False
        for name, collector in dynatrace.items():
            print(f"\n  --- {name} ---")
            try:
                narrow = _query_entities(collector, ip=ip, from_days=None)
            except Exception as exc:  # noqa: BLE001 — the error IS the diagnosis
                print(f"  default-window query FAILED: {exc}")
                continue
            try:
                wide = _query_entities(collector, ip=ip, from_days=_WIDE_DAYS)
            except Exception as exc:  # noqa: BLE001
                print(f"  {_WIDE_DAYS}-day query FAILED: {exc}")
                continue

            if not narrow and not wide:
                print(f"  no HOST entity with this address on {name}, even over "
                      f"{_WIDE_DAYS} days.")
                print("  -> not a time-window issue. Check: the API token's scope "
                      "(entities.read), whether this tenant restricts the token "
                      "to specific management zones, and whether this address "
                      "actually belongs to a different Dynatrace environment "
                      "than the one configured for this instance.")
                continue

            found_anywhere = True
            if wide and not narrow:
                print("  FOUND only with the wide window: this is the "
                      "DYNATRACE_ENTITY_LOOKBACK_DAYS issue. A build sending "
                      "no explicit 'from' would have missed this host.")
            elif narrow:
                print("  found with the default window too. A current build "
                      "should already be collecting this host — if it is "
                      "still missing from SAMI'X, wait for the next poll or "
                      "trigger one from the Overview page.")

            for entity in wide or narrow:
                _describe(entity, ip)

        if not found_anywhere:
            print(f"\n  Verdict for {ip}: not visible to any configured Dynatrace "
                  f"instance under any window tried.")

    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.dynatrace_probe",
        description="Diagnose why a host visible in Dynatrace is missing from SAMI'X.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ip", nargs="+", metavar="IP", help="One or more addresses.")
    group.add_argument(
        "--ip-range", metavar="START-END",
        help='An inclusive range, e.g. "10.22.68.11-10.22.68.20".',
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    settings = get_settings()
    if settings.mock_mode:
        logger.error(
            "MOCK_MODE is on, so there is no Dynatrace to query. Set MOCK_MODE=false "
            "in .env and try again."
        )
        return 2

    try:
        ips = _expand_ip_range(args.ip_range) if args.ip_range else list(args.ip)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    from app.db import SessionLocal, init_db

    init_db()
    db: Session = SessionLocal()
    try:
        probe(db, ips)
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
