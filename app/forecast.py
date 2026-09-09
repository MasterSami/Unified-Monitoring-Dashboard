"""Capacity forecasting — a straight line through recent history, honestly labelled.

The method is ordinary least squares on ``used_pct`` against days, fitted per
series over :attr:`~app.config.Settings.forecast_window_days` of
:class:`~app.models.CapacityHistory`. No model, no library beyond numpy: a disk
that has filled at a steady rate for a month is the one case a linear
extrapolation genuinely answers, and it is the case operators keep being paged
for at 03:00.

What the code spends its care on is knowing when *not* to answer:

* a series with too few points, too short a span, or a host that stopped
  reporting is skipped with a reason rather than fitted;
* a volume that was resized mid-window is fitted only from the resize onward,
  because the samples before it describe a different disk;
* a fit whose R² is below :attr:`~app.config.Settings.forecast_min_r_squared`
  keeps its slope but loses its dates — the data does not sit on a line, so a
  date derived from that line would be invented precision.

Results land in :class:`~app.models.CapacityForecast`, one row per series,
replaced on each run. Pages read that table and never fit anything themselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capacity_history import prune_history
from app.config import Settings, get_settings
from app.models import CapacityForecast, CapacityHistory, Host, HostStatus

logger = logging.getLogger("forecast")

#: Utilization the ETA is quoted against. 90% is where filesystems start
#: misbehaving well before they are actually full.
THRESHOLD_PCT = 90.0

#: Classification bands, in days to THRESHOLD_PCT.
CRITICAL_DAYS, WARNING_DAYS, WATCH_DAYS = 14.0, 45.0, 90.0

#: Beyond ten years a linear trend is arithmetic, not a forecast. The ETA is
#: dropped rather than shown as a number nobody should read.
MAX_HORIZON_DAYS = 3650.0

#: Headroom, in percentage points, below which a volume counts as having
#: arrived rather than as approaching.
#:
#: An ETA is ``headroom / slope``, and both shrink towards nothing as a disk
#: fills. A volume at 99.99% creeping at 0.0008 %/day divides 0.01 by 0.0008
#: and reports "13 days to full" — a number assembled entirely from digits too
#: small to display, which the UI then rounds to "100% · 0.00%/day · 13d". The
#: next poll would move it to 400 days or to 3, because at that scale the
#: division is measuring rounding error. Under half a percentage point of
#: headroom the honest answer is "now": the disk is full, and what it needs is
#: attention, not a date.
MIN_HEADROOM_PCT = 0.5

#: A host not seen for this long is not reporting capacity either; its last
#: samples describe the past, not a trend.
STALE_AFTER_DAYS = 2

CRITICAL, WARNING, WATCH, OK = "critical", "warning", "watch", "ok"
NOISY, INSUFFICIENT = "noisy", "insufficient_data"

#: Classifications that belong on the "at risk" list and in the KPI count.
AT_RISK = (CRITICAL, WARNING)

#: Series kinds that are forecast. CPU is sampled into history for context but
#: deliberately not fitted: a CPU percentage oscillates around a workload, it
#: does not fill up, so "days until CPU is 90%" is a category error. Disk and
#: memory are cumulative enough for a trend line to mean something.
FORECAST_KINDS = ("disk", "memory")


@dataclass
class SeriesFit:
    """The outcome of fitting one series — or the reason there is none."""

    classification: str
    current_pct: float | None = None
    slope_pct_per_day: float | None = None
    r_squared: float | None = None
    days_to_threshold_90: float | None = None
    days_to_full: float | None = None
    total_value: float | None = None
    sample_count: int = 0
    reason: str | None = None
    points: list[list[float]] = field(default_factory=list)


def _daily_means(
    stamps: list[datetime], pcts: list[float], totals: list[float | None]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Collapse raw samples to one point per calendar day (UTC).

    Hourly sampling would otherwise let a day with more samples pull the line
    towards itself, and a poll outage would silently reweight the window.
    Returns ``(day_offsets, mean_pct, mean_total)`` sorted by day, where day 0
    is the earliest day in the window.
    """
    buckets: dict[int, list[tuple[float, float | None]]] = {}
    if not stamps:
        return np.empty(0), np.empty(0), np.empty(0)
    base = min(stamps).replace(hour=0, minute=0, second=0, microsecond=0)
    for stamp, pct, total in zip(stamps, pcts, totals):
        day = (stamp - base).days
        buckets.setdefault(day, []).append((pct, total))

    days = sorted(buckets)
    mean_pct = np.array([np.mean([p for p, _ in buckets[d]]) for d in days], float)
    mean_total = np.array(
        [
            np.mean([t for _, t in buckets[d] if t is not None])
            if any(t is not None for _, t in buckets[d])
            else np.nan
            for d in days
        ],
        float,
    )
    return np.array(days, float), mean_pct, mean_total


def _trim_to_last_resize(
    days: np.ndarray, pcts: np.ndarray, totals: np.ndarray, tolerance: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool]:
    """Drop everything before the most recent change in volume size.

    A disk extended from 100 GB to 500 GB shows a cliff in ``used_pct`` that has
    nothing to do with how fast it is filling; fitting across it produces a
    steep negative slope and a reassuring "never" for a volume that may be
    filling faster than before. Only the samples describing the *current* size
    are kept.
    """
    known = ~np.isnan(totals)
    if known.sum() < 2:
        return days, pcts, totals, False

    idx = np.flatnonzero(known)
    cut = 0
    for prev, cur in zip(idx[:-1], idx[1:]):
        base = totals[prev]
        if base and abs(totals[cur] - base) / abs(base) > tolerance:
            cut = int(cur)
    if cut == 0:
        return days, pcts, totals, False
    return days[cut:], pcts[cut:], totals[cut:], True


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Return ``(slope, intercept, r_squared)`` for a least-squares line.

    A perfectly flat series has no variance to explain; the line through it is
    exact, so R² is 1.0 rather than undefined. That matters: a genuinely stable
    disk should read as well-described, not as noise.
    """
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 if ss_tot == 0.0 else 1.0 - ss_res / ss_tot
    return float(slope), float(intercept), max(0.0, min(1.0, r_squared))


def _eta(current: float, target: float, slope: float) -> float | None:
    """Days until ``current`` reaches ``target`` at ``slope`` %/day.

    ``0.0`` means "already there". ``None`` means there is no date to give —
    either the series is not rising, or the crossing is past the horizon.
    """
    if slope <= 0:
        return None
    # Already at or within touching distance of the target. Extrapolating the
    # last fraction of a percent divides one immeasurably small number by
    # another and produces a date that changes wildly between polls.
    if target - current <= MIN_HEADROOM_PCT:
        return 0.0
    days = (target - current) / slope
    return None if days > MAX_HORIZON_DAYS else round(days, 1)


def fit_series(
    stamps: list[datetime],
    pcts: list[float],
    totals: list[float | None],
    *,
    settings: Settings | None = None,
    host_ok: bool = True,
    host_reason: str = "",
) -> SeriesFit:
    """Fit one series and classify it. Pure: no database, no clock.

    This is the whole method in one function, so the rules can be tested
    directly against synthetic series rather than inferred from a table.
    """
    settings = settings or get_settings()

    if not host_ok:
        return SeriesFit(INSUFFICIENT, reason=host_reason or "host not reporting")

    days, mean_pct, mean_total = _daily_means(stamps, pcts, totals)

    if days.size < settings.forecast_min_points:
        return SeriesFit(
            INSUFFICIENT,
            sample_count=int(days.size),
            reason=f"only {days.size} daily point(s); needs {settings.forecast_min_points}",
        )

    span = float(days[-1] - days[0])
    if span < settings.forecast_min_span_days:
        return SeriesFit(
            INSUFFICIENT,
            sample_count=int(days.size),
            reason=f"history spans {span:.0f} day(s); needs {settings.forecast_min_span_days}",
        )

    days, mean_pct, mean_total, resized = _trim_to_last_resize(
        days, mean_pct, mean_total, settings.forecast_resize_tolerance
    )
    if resized and days.size < settings.forecast_min_points:
        return SeriesFit(
            INSUFFICIENT,
            sample_count=int(days.size),
            reason=(
                f"resized recently; only {days.size} daily point(s) since"
            ),
        )

    latest_total = None
    known = ~np.isnan(mean_total)
    if known.any():
        latest_total = float(mean_total[known][-1])
        if latest_total == 0:
            return SeriesFit(
                INSUFFICIENT,
                sample_count=int(days.size),
                reason="reported size is zero",
            )

    slope, _intercept, r_squared = _ols(days, mean_pct)
    current = float(mean_pct[-1])
    points = [[float(d - days[0]), round(float(p), 2)] for d, p in zip(days, mean_pct)]

    base = SeriesFit(
        OK,
        current_pct=round(current, 2),
        slope_pct_per_day=round(slope, 4),
        r_squared=round(r_squared, 3),
        total_value=latest_total,
        sample_count=int(days.size),
        points=points,
        reason="fitted from the most recent resize onward" if resized else None,
    )

    # Already over the line. This is not a forecast at all — it is a report of
    # the present — and it outranks everything the trend could say, including
    # a flat or falling slope. A volume sitting at 97% for a month is the most
    # urgent row on the page; classifying it "ok" because it stopped growing
    # would bury the one thing somebody has to deal with today.
    if current >= THRESHOLD_PCT:
        base.classification = CRITICAL
        base.days_to_threshold_90 = 0.0
        full = _eta(current, 100.0, slope) if slope > 0 else None
        # A "full in 7 years" beside a disk that is already critical is noise;
        # past the watch horizon the useful statement is just that it is over
        # the line and not moving.
        base.days_to_full = full if (full is not None and full < WATCH_DAYS) else None
        base.reason = f"already at {current:.1f}% — above the {THRESHOLD_PCT:.0f}% line"
        return base

    # Not filling: there is no date to give, so the confidence in the slope
    # does not matter and the series is simply fine.
    if slope <= 0:
        return base

    eta = _eta(current, THRESHOLD_PCT, slope)
    confident = r_squared >= settings.forecast_min_r_squared

    # Rising so slowly that the crossing is past the horizon entirely.
    if eta is None:
        return base

    # Rising, but the crossing is further out than anyone plans for. The answer
    # is "not filling up on a timescale you care about", which holds whether or
    # not the points sit on a line — so the fit quality does not change the
    # verdict, only whether the date is worth printing beside it. This is what
    # keeps a flat, jittery series (memory on an idle host) out of the noisy
    # list on the strength of a slope of a hundredth of a percent a day.
    if eta >= WATCH_DAYS:
        if confident:
            base.days_to_threshold_90 = eta
            base.days_to_full = _eta(current, 100.0, slope)
        return base

    # Filling on a timescale that matters, but the points do not sit on the
    # line. Keep the slope as a hint and withhold the dates — a confident date
    # from a scattered series is the one output that would actively mislead,
    # because it is precise enough to schedule against and wrong.
    if not confident:
        base.classification = NOISY
        base.reason = (
            f"R² {r_squared:.2f} below {settings.forecast_min_r_squared:g}; "
            "trend too scattered to date"
        )
        return base

    base.days_to_threshold_90 = eta
    base.days_to_full = _eta(current, 100.0, slope)
    if eta < CRITICAL_DAYS:
        base.classification = CRITICAL
    elif eta < WARNING_DAYS:
        base.classification = WARNING
    else:
        base.classification = WATCH
    return base


# --- Batch run --------------------------------------------------------------

#: Hosts per history query. Bounds peak memory on a large estate without
#: dropping to a query per series.
_HOST_BATCH = 200


def _host_gate(host: Host, now: datetime) -> tuple[bool, str]:
    """Whether a host's series are worth fitting, and why not when they aren't."""
    if host.status in (HostStatus.unknown, HostStatus.disabled):
        return False, f"host status is {host.status.value}"
    seen = host.last_seen
    if seen is not None:
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if now - seen > timedelta(days=STALE_AFTER_DAYS):
            return False, f"host last seen {(now - seen).days} days ago"
    return True, ""


def run_forecast(
    db: Session, *, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, int]:
    """Refit every series and replace :class:`CapacityForecast`. Returns counts.

    One query per batch of hosts, then all arithmetic in numpy — never a query
    per series.
    """
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=max(1, settings.forecast_window_days))

    hosts = {h.id: h for h in db.scalars(select(Host)).all()}
    host_ids = sorted(hosts)
    counts: dict[str, int] = {}
    rows: list[dict] = []

    for start in range(0, len(host_ids), _HOST_BATCH):
        batch = host_ids[start : start + _HOST_BATCH]
        samples = db.execute(
            select(
                CapacityHistory.host_id,
                CapacityHistory.platform,
                CapacityHistory.metric_kind,
                CapacityHistory.subject,
                CapacityHistory.used_pct,
                CapacityHistory.total_value,
                CapacityHistory.sampled_at,
            )
            .where(
                CapacityHistory.host_id.in_(batch),
                CapacityHistory.sampled_at >= window_start,
                CapacityHistory.metric_kind.in_(FORECAST_KINDS),
            )
            .order_by(
                CapacityHistory.host_id,
                CapacityHistory.metric_kind,
                CapacityHistory.subject,
                CapacityHistory.sampled_at,
            )
        ).all()

        grouped: dict[tuple[int, str, str, str], list[tuple]] = {}
        for host_id, platform, kind, subject, pct, total, stamp in samples:
            grouped.setdefault((host_id, platform, kind, subject), []).append(
                (stamp, pct, total)
            )

        for (host_id, platform, kind, subject), series in grouped.items():
            host = hosts.get(host_id)
            if host is None:
                continue
            ok, why = _host_gate(host, now)
            stamps = [
                s.replace(tzinfo=timezone.utc) if s.tzinfo is None else s
                for s, _, _ in series
            ]
            fit = fit_series(
                stamps,
                [p for _, p, _ in series],
                [t for _, _, t in series],
                settings=settings,
                host_ok=ok,
                host_reason=why,
            )
            counts[fit.classification] = counts.get(fit.classification, 0) + 1
            rows.append(
                {
                    "host_id": host_id,
                    "platform": platform,
                    "metric_kind": kind,
                    "subject": subject,
                    "current_pct": fit.current_pct,
                    "slope_pct_per_day": fit.slope_pct_per_day,
                    "r_squared": fit.r_squared,
                    "days_to_threshold_90": fit.days_to_threshold_90,
                    "days_to_full": fit.days_to_full,
                    "classification": fit.classification,
                    "reason": fit.reason[:255] if fit.reason else None,
                    "points": fit.points,
                    "total_value": fit.total_value,
                    "sample_count": fit.sample_count,
                    "computed_at": now,
                }
            )

    # Replace wholesale: a series that stopped reporting should disappear from
    # the page, not sit there with last month's date on it.
    db.query(CapacityForecast).delete(synchronize_session=False)
    if rows:
        db.bulk_insert_mappings(CapacityForecast, rows)
    db.commit()

    counts["total"] = len(rows)
    logger.info(
        "forecast: %d series over %d day window (%s)",
        len(rows),
        settings.forecast_window_days,
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k != "total") or "none",
    )
    return counts


def run_forecast_job() -> None:
    """Scheduler entry point: prune old samples, then refit. Never raises."""
    db: Session = None  # type: ignore[assignment]
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        prune_history(db)
        run_forecast(db)
    except Exception:  # noqa: BLE001 — must never stop the scheduler
        logger.exception("capacity forecast run failed")
        if db is not None:
            db.rollback()
    finally:
        if db is not None:
            db.close()


# --- Read paths used by the pages -------------------------------------------


def risk_counts(db: Session) -> dict[str, int]:
    """Counts per classification, for the Overview KPI card (one query)."""
    from sqlalchemy import func

    rows = db.execute(
        select(CapacityForecast.classification, func.count(CapacityForecast.id))
        .group_by(CapacityForecast.classification)
    ).all()
    counts = {name: int(n) for name, n in rows}
    counts["at_risk"] = sum(counts.get(c, 0) for c in AT_RISK)
    return counts


def forecast_for_host(db: Session, host_id: int) -> dict[str, CapacityForecast]:
    """Every forecast for one host, keyed by ``"kind:subject"`` (one query).

    Used by the Capacity detail panel, which wants the line for a specific
    mount it is already rendering.
    """
    rows = db.scalars(
        select(CapacityForecast).where(CapacityForecast.host_id == host_id)
    ).all()
    return {f"{r.metric_kind}:{r.subject}": r for r in rows}


def describe(row: CapacityForecast) -> str:
    """One-line plain-English summary, e.g. "At current trend: 90% in ~23 days"."""
    if row.classification == INSUFFICIENT:
        return f"No forecast — {row.reason or 'not enough history'}"
    if row.classification == NOISY:
        return f"Trend too scattered to date (R² {row.r_squared:.2f})"

    # Already over the line — describe the present. Saying "stable" about a
    # volume sitting at 100% is true of its trend and useless to the reader.
    current = row.current_pct
    if current is not None and current >= THRESHOLD_PCT:
        if 100.0 - current <= MIN_HEADROOM_PCT:
            return f"Full now — {current:.1f}%, no space left"
        full = row.days_to_full
        if full is not None and full < WATCH_DAYS:
            return f"Already {current:.1f}% — full in ~{full:.0f} days"
        rising = (row.slope_pct_per_day or 0.0) > 0.005
        tail = "and still rising" if rising else "not growing"
        return f"Already {current:.1f}% — above the {THRESHOLD_PCT:.0f}% line, {tail}"

    if row.slope_pct_per_day is not None and row.slope_pct_per_day <= 0:
        return "At current trend: stable or shrinking"
    if row.days_to_threshold_90 is None:
        return "At current trend: no threshold crossing in range"
    days = row.days_to_threshold_90
    when = "today" if days < 1 else f"in ~{days:.0f} day{'s' if days >= 2 else ''}"
    return f"At current trend: {THRESHOLD_PCT:.0f}% {when}"
