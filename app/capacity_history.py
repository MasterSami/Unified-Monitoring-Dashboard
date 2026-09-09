"""Capacity sampling — turn each poll into rows the forecaster can fit.

:class:`~app.models.Host` carries only the latest reading; every poll
overwrites it. This module appends the reading to
:class:`~app.models.CapacityHistory` first, so that a month later there is
something to draw a line through.

Two shapes of drive data arrive here:

* **structured** — ``metrics["filesystems"] = [{subject, used_gb, total_gb}]``,
  which the Zabbix collector now emits because it already had the per-mount
  values in hand and was throwing them away;
* **packed strings** — the form the exports have always used, e.g. Zabbix's
  ``FS [/var/lib]: Space : 2028.7`` and Dynatrace's
  ``C:\\: 80.1 / 114.4 GB | D:\\: 61.7 / 150.0 GB``.

Both are parsed into ``(subject, used, total)`` triples by the functions below,
so a host whose breakdown only ever existed as a report string still gets
per-drive history.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CapacityHistory, Host

logger = logging.getLogger("capacity.history")

#: The three series kinds. ``disk`` is per-drive; the others are host-level.
DISK, MEMORY, CPU = "disk", "memory", "cpu"

#: Host-level series carry an empty subject — never NULL. See CapacityHistory.
NO_SUBJECT = ""

# --- Packed-string parsers --------------------------------------------------

#: ``<label> : <number>`` — the label is greedy so the LAST colon wins, which
#: is what keeps ``FS [C:]: Space : 114.4`` from splitting at the drive letter.
_ZBX_CELL = re.compile(r"^(?P<label>.+?)\s*:\s*(?P<value>-?\d+(?:[.,]\d+)?)\s*$")

#: Trailing metric words Zabbix appends to an item name. Stripped so
#: ``FS [/var]: Space`` and ``/var`` are recognised as the same mount.
_ZBX_SUFFIX = re.compile(
    r"\s*:\s*(space utilization|total space|used space|free space|"
    r"space|total|used|pused|pfree)\s*$",
    re.I,
)

#: ``FS [/var/lib]`` / ``Filesystem [C:]`` -> the mount point inside.
_ZBX_WRAPPER = re.compile(
    r"^(?:FS|Filesystem|Mounted filesystem)\s*\[(?P<inner>.+)\]$", re.I
)

#: ``C:\: 80.1 / 114.4 GB`` — subject greedy for the same reason as above, so a
#: Windows drive letter's own colon is not mistaken for the separator.
_DT_DISK = re.compile(
    r"^(?P<subject>.+):\s*(?P<used>-?\d+(?:[.,]\d+)?)\s*/\s*"
    r"(?P<total>-?\d+(?:[.,]\d+)?)\s*(?:GB|GiB|MB|TB)?\s*$",
    re.I,
)


def _num(raw: str) -> float | None:
    """Parse a number that may use a comma as its decimal separator."""
    try:
        return float(raw.replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalize_subject(label: str) -> str:
    """Reduce a drive label to the mount point operators would recognise.

    ``FS [/var/lib]: Space`` and ``/var/lib`` both become ``/var/lib``, so the
    same volume keeps one series whether its samples arrived structured, from a
    Zabbix report string, or from a Dynatrace export.
    """
    label = (label or "").strip().strip('"')
    # Strip repeatedly: a name can carry both a wrapper and a metric suffix.
    for _ in range(3):
        stripped = _ZBX_SUFFIX.sub("", label).strip()
        m = _ZBX_WRAPPER.match(stripped)
        if m:
            stripped = m.group("inner").strip()
        if stripped == label:
            break
        label = stripped
    return label.rstrip("\\") if len(label.rstrip("\\")) > 1 else label


def parse_zabbix_drive_cell(cell: object) -> tuple[str, float] | None:
    """``"FS [/var/lib]: Space : 2028.7"`` -> ``("/var/lib", 2028.7)``.

    Returns ``None`` for anything that is not a labelled number — including the
    ``"N/A"`` the weekly report writes for a host with no filesystem items.
    """
    if not isinstance(cell, str):
        return None
    m = _ZBX_CELL.match(cell.strip())
    if not m:
        return None
    value = _num(m.group("value"))
    subject = normalize_subject(m.group("label"))
    if value is None or not subject:
        return None
    return subject, value


def parse_zabbix_drive_pair(
    space_cell: object, used_cell: object
) -> tuple[str, float, float] | None:
    """Pair the two Zabbix report columns into ``(subject, used, total)``.

    ``VM Space For Each Drive`` is the total and ``VM Used Space For Each
    Drive`` the used figure for the *same* mount, one row per drive. They are
    only trusted together when both name the same subject — a mismatch means
    the row was assembled wrong and a used/total ratio from it would be
    meaningless.
    """
    total = parse_zabbix_drive_cell(space_cell)
    used = parse_zabbix_drive_cell(used_cell)
    if total is None or used is None or total[0] != used[0]:
        return None
    return total[0], used[1], total[1]


def parse_dynatrace_disks(packed: object) -> list[tuple[str, float, float]]:
    """``"C:\\: 80.1 / 114.4 GB | D:\\: 61.7 / 150.0 GB"`` -> triples.

    Unparseable segments are skipped rather than failing the whole cell, so one
    odd drive name never costs a host its other volumes.
    """
    if not isinstance(packed, str) or not packed.strip():
        return []
    out: list[tuple[str, float, float]] = []
    for part in packed.split("|"):
        part = part.strip()
        if not part:
            continue
        m = _DT_DISK.match(part)
        if not m:
            continue
        used, total = _num(m.group("used")), _num(m.group("total"))
        subject = normalize_subject(m.group("subject"))
        if used is None or total is None or not subject:
            continue
        out.append((subject, used, total))
    return out


# --- Turning one collected host into series ---------------------------------


def _pct(used: float | None, total: float | None, fallback: float | None) -> float | None:
    """Percentage from absolutes when possible, else the collector's own."""
    if used is not None and total:
        return round(used / total * 100, 2)
    return fallback


def series_for_host(item: dict) -> list[tuple[str, str, float | None, float | None, float]]:
    """Return ``(metric_kind, subject, used, total, used_pct)`` for one host.

    Accepts a normalized collector dict. Series without a usable percentage are
    dropped here rather than stored as nulls the forecaster would have to skip.
    """
    metrics = item.get("metrics") or {}
    rows: list[tuple[str, str, float | None, float | None, float]] = []

    cpu_pct = item.get("cpu_pct")
    if cpu_pct is not None:
        rows.append(
            (CPU, NO_SUBJECT, metrics.get("cpu_used_cores"), metrics.get("cores"),
             float(cpu_pct))
        )

    mem_pct = _pct(metrics.get("mem_used_gb"), metrics.get("mem_total_gb"),
                   item.get("mem_pct"))
    if mem_pct is not None:
        rows.append(
            (MEMORY, NO_SUBJECT, metrics.get("mem_used_gb"),
             metrics.get("mem_total_gb"), float(mem_pct))
        )

    # Per-drive disk, from whichever shape this collector produced.
    drives: list[tuple[str, float, float]] = []
    for fs in metrics.get("filesystems") or []:
        subject = normalize_subject(str(fs.get("subject") or ""))
        used, total = fs.get("used_gb"), fs.get("total_gb")
        if subject and used is not None and total:
            drives.append((subject, float(used), float(total)))
    if not drives:
        drives = parse_dynatrace_disks(metrics.get("disks_packed"))

    seen: set[str] = set()
    for subject, used, total in drives:
        if subject in seen or not total:
            continue
        seen.add(subject)
        rows.append((DISK, subject, used, total, round(used / total * 100, 2)))

    # Host-level disk, so a host with no per-drive breakdown still trends.
    disk_pct = _pct(metrics.get("disk_used_gb"), metrics.get("disk_total_gb"),
                    item.get("disk_pct"))
    if disk_pct is not None and not drives:
        rows.append(
            (DISK, NO_SUBJECT, metrics.get("disk_used_gb"),
             metrics.get("disk_total_gb"), float(disk_pct))
        )
    return rows


def _hosts_without_history(db: Session, host_ids: list[int]) -> set[int]:
    """Host ids that have no capacity samples at all (one query)."""
    if not host_ids:
        return set()
    known = {
        r[0]
        for r in db.execute(
            select(CapacityHistory.host_id)
            .where(CapacityHistory.host_id.in_(host_ids))
            .group_by(CapacityHistory.host_id)
        ).all()
    }
    return set(host_ids) - known


def seed_mock_history(
    db: Session,
    platform: str,
    rows_by_external_id: dict[str, Host],
    now: datetime,
) -> int:
    """Generate synthetic history for mock hosts seen for the first time.

    ``MOCK_MODE`` exists so the UI can be built and demoed without VPN access,
    and a forecast page needs a month of history before it shows anything at
    all. Each mock host is given the 34 days leading up to today, ending exactly
    on the value its Capacity row displays, following one of the trend profiles
    in :mod:`app.collectors.mock_data` — so a demo shows a filling disk, a
    stable one, a shrinking one, a noisy one and one resized mid-window.

    Only ever fills in hosts with no history, so it runs once per host and never
    fights the live sampler.
    """
    from app.collectors.mock_data import (
        MOCK_HISTORY_DAYS,
        _jitter,
        profile_point,
        trend_profile,
    )

    fresh = _hosts_without_history(
        db, sorted({r.id for r in rows_by_external_id.values() if r.id})
    )
    if not fresh:
        return 0

    pending: list[dict] = []
    for external_id, row in rows_by_external_id.items():
        if row.id not in fresh:
            continue
        # Mock external ids are "<instance-slug>-h<n>"; the index picks the
        # profile, exactly as it did when the host's metrics were generated.
        tail = external_id.rsplit("-h", 1)[-1]
        if not tail.isdigit():
            continue
        idx = int(tail)
        profile = trend_profile(row.hostname, idx, row.status)
        if profile is None:
            continue

        for day_offset in range(1, MOCK_HISTORY_DAYS):
            stamp = now - timedelta(days=day_offset)
            pct, total_gb = profile_point(profile, day_offset)
            pct = max(1.0, min(99.5, pct + _jitter(external_id, day_offset,
                                                   profile["noise"])))
            pending.append(
                {
                    "host_id": row.id,
                    "platform": platform,
                    "metric_kind": DISK,
                    "subject": profile["subject"],
                    "used_value": round(total_gb * pct / 100, 2),
                    "total_value": total_gb,
                    "used_pct": round(pct, 2),
                    "sampled_at": stamp,
                }
            )
            # A flat memory series per host, ending on the value the host is
            # actually reporting today. Anchoring it anywhere else puts a step
            # between the synthetic past and the live present, which shows up
            # as a spurious spike at the right-hand end of every sparkline.
            if row.mem_pct is not None:
                pending.append(
                    {
                        "host_id": row.id,
                        "platform": platform,
                        "metric_kind": MEMORY,
                        "subject": NO_SUBJECT,
                        "used_value": None,
                        "total_value": None,
                        "used_pct": round(
                            min(99.5, max(1.0, row.mem_pct
                                          + _jitter(external_id + "m", day_offset, 1.5))),
                            2,
                        ),
                        "sampled_at": stamp,
                    }
                )

    if pending:
        db.bulk_insert_mappings(CapacityHistory, pending)
        logger.info(
            "seeded %d synthetic capacity sample(s) for %d mock host(s)",
            len(pending), len(fresh),
        )
    return len(pending)


def _recently_sampled(db: Session, host_ids: list[int], cutoff: datetime) -> set[int]:
    """Host ids that already have a sample at or after ``cutoff`` (one query)."""
    if not host_ids:
        return set()
    rows = db.execute(
        select(CapacityHistory.host_id)
        .where(
            CapacityHistory.host_id.in_(host_ids),
            CapacityHistory.sampled_at >= cutoff,
        )
        .group_by(CapacityHistory.host_id)
    ).all()
    return {r[0] for r in rows}


def record_samples(
    db: Session,
    platform: str,
    items: list[dict],
    rows_by_external_id: dict[str, Host],
    *,
    now: datetime | None = None,
) -> int:
    """Append this poll's capacity readings. Returns the row count written.

    Throttled to one sample per host per ``CAPACITY_HISTORY_MIN_MINUTES``: the
    forecast resamples to one point per day, so storing all 288 of a five-minute
    poll cycle's readings would cost 12x the rows for no extra resolution.

    Never raises — a capacity sample is worth strictly less than the collection
    run carrying it, so a failure here is logged and the run continues.
    """
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    written = 0

    try:
        pending: list[dict] = []
        for item in items:
            row = rows_by_external_id.get(str(item.get("external_id")))
            if row is None or row.id is None:
                continue
            for kind, subject, used, total, pct in series_for_host(item):
                pending.append(
                    {
                        "host_id": row.id,
                        "platform": platform,
                        "metric_kind": kind,
                        "subject": subject,
                        "used_value": used,
                        "total_value": total,
                        "used_pct": pct,
                        "sampled_at": now,
                    }
                )
        if not pending:
            return 0

        # In a mock demo there is no real past to draw on, so the first sight of
        # a host also writes the month behind it.
        if settings.mock_mode:
            written += seed_mock_history(db, platform, rows_by_external_id, now)

        gap = max(0, int(settings.capacity_history_min_minutes))
        if gap:
            fresh = _recently_sampled(
                db,
                sorted({p["host_id"] for p in pending}),
                now - timedelta(minutes=gap),
            )
            pending = [p for p in pending if p["host_id"] not in fresh]

        if pending:
            db.bulk_insert_mappings(CapacityHistory, pending)
            written = len(pending)
    except Exception:  # noqa: BLE001 — sampling must never fail a collection
        logger.warning("capacity sampling failed for %s", platform, exc_info=True)
        return 0
    return written


def prune_history(db: Session, *, now: datetime | None = None) -> int:
    """Delete samples older than the retention window. Returns rows removed."""
    settings = get_settings()
    days = max(1, int(settings.capacity_history_retention_days))
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    deleted = (
        db.query(CapacityHistory)
        .filter(CapacityHistory.sampled_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info("pruned %d capacity sample(s) older than %d days", deleted, days)
    return int(deleted or 0)


def sample_count(db: Session) -> int:
    """Total stored samples — shown on /forecast so an empty page explains itself."""
    return int(db.scalar(select(func.count(CapacityHistory.id))) or 0)
