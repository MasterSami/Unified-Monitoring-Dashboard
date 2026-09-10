"""One-off backfill of capacity history from Zabbix trends.

    python -m app.backfill_zabbix_capacity --days 90

Forecasting needs history, and history only starts accumulating the day the
sampling ships. Zabbix already keeps daily aggregates in ``trends`` (and
``trends_uint``) for exactly the items the Capacity page reads, so a single
run of this command gives every Zabbix host a usable trend line immediately
rather than seven weeks from now.

Design notes:

* **Idempotent.** Rows are keyed on ``(host, metric, subject, day)`` with the
  timestamp normalized to midnight UTC, and existing keys are read back before
  inserting. Running it twice, or extending an earlier run's window, adds only
  what is missing.
* **Resumable.** Work is committed per host, so an interrupted run keeps
  everything it had already written and the next run skips it cheaply.
* **Polite.** Hosts are walked one at a time with a short sleep between them;
  ``trend.get`` over 90 days is a heavy query and this is a background chore,
  not an outage.

Dynatrace is supported behind ``--dynatrace`` / ``FORECAST_DYNATRACE_BACKFILL``
but is expected to fail on most estates: the Metrics v2 API needs the
``metrics.read`` scope that the dashboard's token usually lacks. That failure is
logged and skipped — forecasting works from whatever history exists.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capacity_history import CPU, DISK, MEMORY, NO_SUBJECT, normalize_subject
from app.config import get_settings
from app.models import CapacityHistory, Host, HostStatus, SourcePlatform

logger = logging.getLogger("backfill.capacity")

GB = 1024.0**3

#: Seconds between hosts. Small, but enough that a 90-day trend walk over a
#: few hundred hosts does not look like a denial of service to the API.
HOST_DELAY_SECONDS = 0.2

#: Items per trend.get call.
_TREND_BATCH = 50


def _midnight(stamp: datetime) -> datetime:
    """Normalize to 00:00 UTC — the key a daily aggregate is stored under."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _daily_avgs(collector, itemids: list[str], days: int) -> dict[str, dict[datetime, float]]:
    """``{itemid: {day_midnight: mean_value}}`` from Zabbix trends.

    Zabbix stores one trend row per item per hour; the daily mean is the
    sample-count-weighted mean of those rows, which is what the forecaster
    would compute from live samples anyway.
    """
    if not itemids:
        return {}
    now = int(time.time())
    out: dict[str, dict[datetime, tuple[float, int]]] = {}
    for start in range(0, len(itemids), _TREND_BATCH):
        chunk = itemids[start : start + _TREND_BATCH]
        rows = collector._rpc(
            "trend.get",
            {
                "itemids": chunk,
                "time_from": now - days * 86400,
                "time_till": now,
                "output": ["itemid", "clock", "num", "value_avg"],
            },
        )
        for row in rows or []:
            avg = row.get("value_avg")
            if avg is None:
                continue
            try:
                day = _midnight(
                    datetime.fromtimestamp(int(row["clock"]), tz=timezone.utc)
                )
                value, count = float(avg), max(1, int(row.get("num") or 1))
            except (TypeError, ValueError, KeyError, OSError):
                continue
            bucket = out.setdefault(str(row["itemid"]), {})
            prev_sum, prev_n = bucket.get(day, (0.0, 0))
            bucket[day] = (prev_sum + value * count, prev_n + count)
    return {
        iid: {day: total / n for day, (total, n) in per_day.items() if n}
        for iid, per_day in out.items()
    }


def _series_for_host(collector, hostid: str, days: int) -> list[tuple]:
    """Return ``(kind, subject, used, total, pct, day)`` rows for one host.

    Reuses the collector's own item classification, so the mounts backfilled
    here are exactly the mounts the live sampler will keep appending to.
    """
    from app.zabbix_report import _KEY_SEARCH, _METRIC_NAME_SEARCH, _classify

    items = collector._rpc(
        "item.get",
        {
            "hostids": [hostid],
            "output": ["itemid", "hostid", "key_", "name", "value_type", "units"],
            "search": {"key_": _KEY_SEARCH, "name": _METRIC_NAME_SEARCH},
            "searchByAny": True,
            "startSearch": True,
            "filter": {"status": 0},
            "webitems": False,
        },
    )
    if not items:
        return []
    classified = _classify(items)

    wanted: list[str] = []
    for key in ("cpu_util", "mem_util", "mem_total", "mem_used"):
        if classified[key]:
            wanted.append(classified[key]["itemid"])
    for slot in classified["fs"].values():
        for key in ("total", "used"):
            if slot[key]:
                wanted.append(slot[key]["itemid"])

    trends = _daily_avgs(collector, wanted, days)

    def per_day(item) -> dict[datetime, float]:
        return trends.get(item["itemid"], {}) if item else {}

    rows: list[tuple] = []

    cpu = per_day(classified["cpu_util"])
    for day, value in cpu.items():
        rows.append((CPU, NO_SUBJECT, None, None, round(value, 2), day))

    mem_util = per_day(classified["mem_util"])
    mem_total = per_day(classified["mem_total"])
    mem_used = per_day(classified["mem_used"])
    for day in sorted(set(mem_util) | set(mem_used)):
        total = mem_total.get(day)
        pct = mem_util.get(day)
        if pct is not None and classified["mem_util_inverted"]:
            pct = 100.0 - pct
        used = mem_used.get(day)
        if pct is None and total and used is not None:
            pct = used / total * 100
        if pct is None:
            continue
        rows.append(
            (
                MEMORY,
                NO_SUBJECT,
                round(used / GB, 2) if used is not None else None,
                round(total / GB, 2) if total else None,
                round(pct, 2),
                day,
            )
        )

    for fsname, slot in classified["fs"].items():
        subject = normalize_subject(slot["label"] or fsname)
        totals = per_day(slot["total"])
        useds = per_day(slot["used"])
        for day in sorted(set(totals) & set(useds)):
            total, used = totals[day], useds[day]
            if not total:
                continue
            rows.append(
                (
                    DISK,
                    subject,
                    round(used / GB, 2),
                    round(total / GB, 2),
                    round(used / total * 100, 2),
                    day,
                )
            )
    return rows


def _existing_keys(db: Session, host_id: int) -> set[tuple[str, str, datetime]]:
    """Keys already stored for a host, so a re-run inserts only what is new."""
    rows = db.execute(
        select(
            CapacityHistory.metric_kind,
            CapacityHistory.subject,
            CapacityHistory.sampled_at,
        ).where(CapacityHistory.host_id == host_id)
    ).all()
    return {(kind, subject, _midnight(stamp)) for kind, subject, stamp in rows}


def backfill_zabbix(db: Session, days: int, *, instance: str | None = None) -> int:
    """Backfill every configured Zabbix instance. Returns rows written."""
    from app.scheduler import get_service

    service = get_service()
    written = 0

    for name, collector in service.collectors.items():
        if getattr(collector, "name", "") != "zabbix":
            continue
        if instance and name != instance:
            continue

        hosts = db.scalars(
            select(Host).where(
                Host.source_platform == SourcePlatform.zabbix,
                Host.source_instance == name,
            )
        ).all()
        logger.info("%s: backfilling %d host(s) over %d days", name, len(hosts), days)

        # Per-instance tally. Without it, "0 rows written" is indistinguishable
        # from "every host already had its rows", "no host has trend data", and
        # "every call failed" — which is exactly the ambiguity that made an
        # empty Planning tab hard to explain.
        no_items = no_trends = already_had = failed = contributed = 0
        oldest_written: datetime | None = None

        for index, host in enumerate(hosts, start=1):
            try:
                seen = _existing_keys(db, host.id)
                rows = _series_for_host(collector, host.external_id, days)
                if not rows:
                    no_trends += 1
                pending = [
                    {
                        "host_id": host.id,
                        "platform": SourcePlatform.zabbix.value,
                        "metric_kind": kind,
                        "subject": subject,
                        "used_value": used,
                        "total_value": total,
                        "used_pct": pct,
                        "sampled_at": day,
                    }
                    for kind, subject, used, total, pct, day in rows
                    if (kind, subject, day) not in seen
                ]
                if pending:
                    db.bulk_insert_mappings(CapacityHistory, pending)
                    # Commit per host so an interrupted run keeps its progress.
                    db.commit()
                    written += len(pending)
                    contributed += 1
                    batch_oldest = min(p["sampled_at"] for p in pending)
                    if oldest_written is None or batch_oldest < oldest_written:
                        oldest_written = batch_oldest
                elif rows:
                    already_had += 1
            except Exception as exc:  # noqa: BLE001 — one host never stops the run
                db.rollback()
                failed += 1
                if failed <= 5:      # the first few carry the reason; the rest repeat it
                    logger.warning("%s: host %s failed: %s", name, host.hostname, exc)
                elif failed == 6:
                    logger.warning("%s: further per-host failures suppressed", name)

            if index % 100 == 0 or index == len(hosts):
                logger.info("%s: %d/%d host(s), %d row(s) written",
                            name, index, len(hosts), written)
            time.sleep(HOST_DELAY_SECONDS)

        # --- Per-instance verdict -------------------------------------------
        logger.info(
            "%s: done. %d host(s) contributed history, %d already had it, "
            "%d returned no trend data, %d failed",
            name, contributed, already_had, no_trends, failed,
        )
        if oldest_written:
            logger.info("%s: oldest sample written %s", name, f"{oldest_written:%Y-%m-%d}")

        if hosts and no_trends >= len(hosts) * 0.8:
            logger.warning(
                "%s: %d of %d host(s) returned NO trend data. The forecast needs "
                "at least %d daily points, so it will stay empty for these. Usual "
                "causes, in order of likelihood: (1) this Zabbix keeps trends for "
                "less time than requested, or housekeeping has trimmed them - try "
                "a smaller --days; (2) the filesystem/memory items do not have "
                "trend storage enabled on their templates; (3) trend.get is "
                "unavailable (Zabbix older than 5.4).",
                name, no_trends, len(hosts), get_settings().forecast_min_points,
            )
        if hosts and failed >= len(hosts) * 0.5:
            logger.error(
                "%s: %d of %d host(s) FAILED outright. The first few warnings "
                "above carry the reason - usually authentication or a permission "
                "on the API user rather than anything about the data.",
                name, failed, len(hosts),
            )

    return written


def backfill_dynatrace(db: Session, days: int) -> int:
    """Best-effort Dynatrace backfill. Expected to be unavailable; never raises.

    The Metrics v2 API needs the ``metrics.read`` scope. Where the token lacks
    it the call 403s exactly as the live capacity collector already does, and
    this returns 0 with a clear log line rather than failing the command.
    """
    from app.scheduler import get_service

    written = 0
    for name, collector in get_service().collectors.items():
        if getattr(collector, "name", "") != "dynatrace":
            continue
        try:
            breakdown = collector.disk_breakdown()
        except Exception as exc:  # noqa: BLE001 — optional by design
            logger.warning(
                "%s: Dynatrace backfill unavailable (%s). Forecasting will use "
                "whatever history the live sampler collects from now on.", name, exc
            )
            continue
        if not breakdown:
            logger.warning(
                "%s: Dynatrace returned no disk breakdown — the token most "
                "likely lacks the metrics.read scope. Skipping; forecasting "
                "still works from live samples.", name
            )
            continue
        # A breakdown without timestamps is a single current reading, not
        # history: record it as today's sample and let the sampler take over.
        today = _midnight(datetime.now(timezone.utc))
        hosts = {
            h.external_id: h
            for h in db.scalars(
                select(Host).where(
                    Host.source_platform == SourcePlatform.dynatrace,
                    Host.source_instance == name,
                )
            ).all()
        }
        pending = []
        for external_id, disks in breakdown.items():
            host = hosts.get(external_id)
            if host is None:
                continue
            seen = _existing_keys(db, host.id)
            for label, total_gb, used_gb in disks:
                subject = normalize_subject(label)
                if not total_gb or (DISK, subject, today) in seen:
                    continue
                pending.append(
                    {
                        "host_id": host.id,
                        "platform": SourcePlatform.dynatrace.value,
                        "metric_kind": DISK,
                        "subject": subject,
                        "used_value": round(used_gb, 2),
                        "total_value": round(total_gb, 2),
                        "used_pct": round(used_gb / total_gb * 100, 2),
                        "sampled_at": today,
                    }
                )
        if pending:
            db.bulk_insert_mappings(CapacityHistory, pending)
            db.commit()
            written += len(pending)
            logger.info("%s: wrote %d Dynatrace sample(s)", name, len(pending))
    return written


def probe(db: Session, limit: int = 3, days: int = 90) -> None:
    """Trace the backfill for a handful of hosts and print what came back.

    A full run walks thousands of hosts before its summary, which is a long
    way to go to discover that ``trend.get`` returns nothing. This does the
    same work for a few hosts and shows each step: the items matched, the
    trend rows returned, and the series that would be stored.
    """
    from app.scheduler import get_service
    from app.zabbix_report import _KEY_SEARCH, _METRIC_NAME_SEARCH, _classify

    for name, collector in get_service().collectors.items():
        if getattr(collector, "name", "") != "zabbix":
            continue
        hosts = db.scalars(
            select(Host).where(
                Host.source_platform == SourcePlatform.zabbix,
                Host.source_instance == name,
                Host.status == HostStatus.up,
            ).limit(limit)
        ).all()
        if not hosts:
            print(f"\n{name}: no 'up' hosts in the database to probe.")
            continue

        print(f"\n=== PROBE {name} ({len(hosts)} host(s), {days} days) ===")
        for host in hosts:
            print(f"\n  host: {host.hostname}  (zabbix id {host.external_id})")
            try:
                items = collector._rpc(
                    "item.get",
                    {
                        "hostids": [host.external_id],
                        "output": ["itemid", "key_", "name", "value_type"],
                        "search": {"key_": _KEY_SEARCH, "name": _METRIC_NAME_SEARCH},
                        "searchByAny": True, "startSearch": True,
                        "filter": {"status": 0}, "webitems": False,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - this is the diagnosis
                print(f"    item.get FAILED: {type(exc).__name__}: {exc}")
                continue

            print(f"    capacity items matched : {len(items or [])}")
            if not items:
                print("    -> no cpu/memory/filesystem items on this host.")
                continue

            classified = _classify(items)
            wanted = [
                classified[k]["itemid"]
                for k in ("cpu_util", "mem_util", "mem_total", "mem_used")
                if classified[k]
            ]
            for slot in classified["fs"].values():
                wanted += [slot[k]["itemid"] for k in ("total", "used") if slot[k]]
            print(f"    filesystems found      : {len(classified['fs'])}"
                  f"  {sorted(classified['fs'])[:4]}")
            print(f"    items to pull trends for: {len(wanted)}")
            if not wanted:
                continue

            try:
                trends = _daily_avgs(collector, wanted, days)
            except Exception as exc:  # noqa: BLE001 - this is the diagnosis
                print(f"    trend.get FAILED: {type(exc).__name__}: {exc}")
                print("    -> trend.get needs Zabbix 5.4+ and an API user with")
                print("       read access to these items.")
                continue

            covered = sum(1 for v in trends.values() if v)
            print(f"    items WITH trend data  : {covered} of {len(wanted)}")
            if not covered:
                print("    -> Zabbix returned NO trends for any item. This is the")
                print("       reason the forecast has no history. Either trends are")
                print("       not stored for these items, or housekeeping has")
                print("       trimmed them. Try a smaller --days.")
                continue

            all_days = sorted({d for per_day in trends.values() for d in per_day})
            print(f"    distinct days returned : {len(all_days)}"
                  f"   {all_days[0]:%Y-%m-%d} .. {all_days[-1]:%Y-%m-%d}")
            rows = _series_for_host(collector, host.external_id, days)
            kinds: dict[str, int] = {}
            for kind, *_rest in rows:
                kinds[kind] = kinds.get(kind, 0) + 1
            print(f"    rows this host would add: {len(rows)}  {kinds}")
            need = get_settings().forecast_min_points
            verdict = "ENOUGH" if len(all_days) >= need else f"NOT ENOUGH (needs {need})"
            print(f"    -> {verdict} for a forecast")
    print()


def print_status(db: Session) -> None:
    """Print what the forecast can actually see, and what it is rejecting.

    An empty Planning tab has several possible causes that look identical from
    the page: no samples at all, samples too recent to fit, or samples the host
    gate is throwing away. This prints enough to tell them apart in one go.
    """
    from sqlalchemy import func

    from app.forecast import STALE_AFTER_DAYS, _host_gate
    from app.models import CapacityForecast

    settings = get_settings()
    now = datetime.now(timezone.utc)

    print("\n=== DATABASE ===")
    print(f"  url          : {settings.database_url}")
    print(f"  cwd          : {__import__('os').getcwd()}")
    print("  (a relative sqlite path resolves against the cwd, so running the")
    print("   app and this command from different folders uses different files)")

    print("\n=== STORED SAMPLES (capacity_history) ===")
    rows = db.execute(
        select(
            CapacityHistory.platform,
            CapacityHistory.metric_kind,
            func.count(CapacityHistory.id),
            func.min(CapacityHistory.sampled_at),
            func.max(CapacityHistory.sampled_at),
        ).group_by(CapacityHistory.platform, CapacityHistory.metric_kind)
    ).all()
    if not rows:
        print("  NONE. Nothing has been sampled or backfilled into this database.")
    for platform, kind, count, oldest, newest in rows:
        span = ""
        if oldest and newest:
            span = f"  spanning {(newest - oldest).days} day(s)"
            span += f"   {oldest:%Y-%m-%d} .. {newest:%Y-%m-%d}"
        print(f"  {platform:12} {kind:7} {count:>8} row(s){span}")

    print("\n=== STORED FORECASTS (capacity_forecast) ===")
    rows = db.execute(
        select(CapacityForecast.classification, func.count(CapacityForecast.id))
        .group_by(CapacityForecast.classification)
    ).all()
    if not rows:
        print("  NONE. Either the forecast has never run, or every series was")
        print("  rejected. The host gate below usually says which.")
    for name, count in sorted(rows):
        print(f"  {name:20} {count:>6}")

    reasons = db.execute(
        select(CapacityForecast.reason, func.count(CapacityForecast.id))
        .where(CapacityForecast.classification == "insufficient_data")
        .group_by(CapacityForecast.reason)
        .order_by(func.count(CapacityForecast.id).desc())
        .limit(6)
    ).all()
    if reasons:
        print("\n  why series were skipped:")
        for reason, count in reasons:
            print(f"    {count:>6}  {reason}")

    print("\n=== HOSTS AND THE STALENESS GATE ===")
    hosts = db.scalars(select(Host)).all()
    by_status: dict[str, int] = {}
    passed = 0
    gate_reasons: dict[str, int] = {}
    for host in hosts:
        by_status[host.status.value] = by_status.get(host.status.value, 0) + 1
        ok, why = _host_gate(host, now)
        if ok:
            passed += 1
        else:
            key = "stale" if "last seen" in why else why
            gate_reasons[key] = gate_reasons.get(key, 0) + 1
    print(f"  hosts total  : {len(hosts)}")
    for status, count in sorted(by_status.items()):
        print(f"    {status:10} {count:>6}")
    print(f"  pass the gate: {passed}")
    for why, count in sorted(gate_reasons.items(), key=lambda kv: -kv[1]):
        print(f"    rejected: {why:28} {count:>6}")
    print(f"  (a host is 'stale' once last_seen is over {STALE_AFTER_DAYS} day(s) old;")
    print("   set FORECAST_STALE_AFTER_DAYS in .env to change that)")

    freshest = db.scalar(select(func.max(Host.last_seen)))
    if freshest is not None:
        if freshest.tzinfo is None:
            freshest = freshest.replace(tzinfo=timezone.utc)
        age = now - freshest
        print(f"  newest last_seen: {freshest:%Y-%m-%d %H:%M} UTC "
              f"({age.days}d {age.seconds // 3600}h ago)")
        if age > timedelta(days=STALE_AFTER_DAYS):
            print("  >> EVERY host is past the staleness gate. Start the app and let")
            print("     one collection finish, then run the forecast again.")

    unknown = by_status.get(HostStatus.unknown.value, 0)
    if unknown and unknown > len(hosts) / 2:
        print("  >> Most hosts are 'unknown'. They are skipped by design; run a")
        print("     collection so their status and last_seen refresh.")
    print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill_zabbix_capacity",
        description="Backfill capacity history from Zabbix trends.",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="How far back to pull daily aggregates (default: 90).",
    )
    parser.add_argument(
        "--instance", default=None,
        help="Only this Zabbix instance (default: every configured one).",
    )
    parser.add_argument(
        "--dynatrace", action="store_true",
        help="Also attempt Dynatrace (needs the metrics.read scope; usually absent).",
    )
    parser.add_argument(
        "--forecast", action="store_true",
        help="Run the forecast immediately after backfilling.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Report what the forecast can see and why series are skipped, "
             "then exit without touching anything.",
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="Trace the backfill for a few hosts and print what Zabbix returns "
             "at each step, then exit. Seconds instead of a full run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    # Read-only, and useful precisely when something looks wrong, so it runs
    # before the mock-mode check and never needs a live Zabbix.
    if args.status:
        from app.db import SessionLocal, init_db

        init_db()
        db = SessionLocal()
        try:
            print_status(db)
        finally:
            db.close()
        return 0

    settings = get_settings()
    if settings.mock_mode:
        logger.error(
            "MOCK_MODE is on, so there is no Zabbix to query. Set MOCK_MODE=false "
            "in .env, or use the synthetic mock history instead."
        )
        return 2

    from app.db import SessionLocal, init_db

    init_db()
    db: Session = SessionLocal()
    try:
        if args.probe:
            probe(db, days=max(1, args.days))
            return 0

        total = backfill_zabbix(db, max(1, args.days), instance=args.instance)
        if args.dynatrace or settings.forecast_dynatrace_backfill:
            total += backfill_dynatrace(db, max(1, args.days))
        logger.info("backfill complete: %d sample(s) written", total)

        if args.forecast:
            from app.forecast import run_forecast

            counts = run_forecast(db)
            logger.info(
                "forecast: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
