"""What-If capacity simulation - the third Capacity view.

Analysis shows what a server uses now, Forecasting shows where that trend
lands, and What-If answers the question that follows: *if we change
something, where does it land instead?* Extend a volume, move a workload,
halve the growth, pick a different alert line - and read off how many days
that buys.

Everything here is an in-memory copy of the current baseline. Nothing is
written to ``capacity_history`` or ``capacity_forecast``; a saved scenario is
only its definition and is re-run against whatever the baseline is when it is
loaded. The projection maths is the scheduled forecast's own
(:func:`app.forecast.compute_projection`), so a scenario can change the
inputs but can never make a noisy series trustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.forecast import MAX_HORIZON_DAYS, compute_projection
from app.models import CapacityForecast, Host
from app.whatif.schemas import Operation, Projection, SimulationRequest

#: Forecast classifications a scenario is not allowed to start from. The
#: forecast page already explains why on the row; What-If repeats the reason
#: rather than pretending a line through scattered points is a baseline.
BLOCKED = frozenset({"noisy", "insufficient_data"})

#: How far the chart looks ahead by default, and the most it will ever draw.
DEFAULT_HORIZON_DAYS = 90
MAX_CHART_DAYS = 365

_LABELS = {
    "add_once": "Add data once",
    "free_once": "Free space once",
    "add_daily": "Extra growth per day",
    "remove_daily": "Less growth per day",
    "growth_multiplier": "Multiply growth rate",
    "resize": "Extend capacity",
    "add_utilization": "Add utilization",
    "utilization_multiplier": "Multiply utilization",
    "threshold": "Change alert threshold",
    "horizon_date": "Fast-forward to a date",
    "move_workload": "Move workload",
    "redistribute": "Redistribute",
}


def op_label(kind: str) -> str:
    return _LABELS.get(kind, kind.replace("_", " ").title())


@dataclass
class State:
    """One series as the simulation sees it. ``used``/``total`` are GB (or
    cores) when the series knows its size, else None and only ``current_pct``
    is meaningful."""

    current_pct: float
    slope: float
    total: float | None
    used: float | None
    kind: str
    subject: str
    points: list[list[float]]
    r_squared: float | None
    trusted: bool = True
    threshold: float = 90.0


def target_key(host_id: int, kind: str, subject: str = "") -> str:
    return f"{host_id}:{kind}:{subject}"


def parse_key(value: str) -> tuple[int, str, str]:
    parts = (value or "").split(":", 2)
    if len(parts) != 3 or not parts[0].isdigit() or parts[1] not in ("disk", "memory", "cpu"):
        raise ValueError("target must look like <host id>:<disk|memory|cpu>:<subject>")
    return int(parts[0]), parts[1], parts[2]


def _unit(kind: str) -> str:
    return "cores" if kind == "cpu" else "GB"


def _host_total(host: Host, kind: str, subject: str) -> float | None:
    """Fallback size from the host's live metrics when the forecast row has
    none (memory series are fitted from percent only)."""
    metrics = host.metrics or {}
    key = {"memory": "mem_total_gb", "cpu": "cores"}.get(kind)
    if kind == "disk" and not subject:
        key = "disk_total_gb"
    value = metrics.get(key) if key else None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _catalog_item(host: Host, forecast: CapacityForecast | None, kind: str, subject: str) -> dict[str, Any]:
    if forecast is not None:
        trusted = forecast.classification not in BLOCKED
        total = forecast.total_value or _host_total(host, kind, subject)
        return {
            "id": target_key(host.id, kind, subject),
            "host_id": host.id,
            "label": f"{host.hostname} · {kind}" + (f" · {subject}" if subject else ""),
            "hostname": host.hostname, "ip": host.ip, "platform": forecast.platform,
            "instance": host.source_instance, "group": host.group_name or "",
            "kind": kind, "subject": subject, "unit": _unit(kind),
            "current_pct": forecast.current_pct, "slope_pct_per_day": forecast.slope_pct_per_day,
            "r_squared": forecast.r_squared, "days_to_threshold": forecast.days_to_threshold_90,
            "days_to_full": forecast.days_to_full, "total_value": total,
            "classification": forecast.classification,
            "trust": "trusted" if trusted else "blocked",
            "reason": forecast.reason,
        }
    # CPU is sampled but never forecast (it oscillates, it does not fill), so
    # it is offered from the live reading as a utilization-only target.
    return {
        "id": target_key(host.id, "cpu", ""), "host_id": host.id,
        "label": f"{host.hostname} · cpu", "hostname": host.hostname, "ip": host.ip,
        "platform": host.source_platform.value, "instance": host.source_instance,
        "group": host.group_name or "", "kind": "cpu", "subject": "", "unit": "cores",
        "current_pct": host.cpu_pct, "slope_pct_per_day": 0.0, "r_squared": None,
        "days_to_threshold": None, "days_to_full": None,
        "total_value": _host_total(host, "cpu", ""), "classification": "ok",
        "trust": "trusted",
        "reason": "CPU is indicative only: utilization scenarios, no days-to-full date.",
    }


def target_catalog(
    db: Session, q: str = "", kind: str = "all", host_id: int | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Searchable list of things a scenario can start from: every forecast
    series (disk, memory) plus a CPU entry per host with a reading."""
    stmt = select(CapacityForecast, Host).join(Host, Host.id == CapacityForecast.host_id)
    if kind != "all":
        stmt = stmt.where(CapacityForecast.metric_kind == kind)
    if host_id is not None:
        stmt = stmt.where(Host.id == host_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Host.hostname.ilike(like), Host.ip.ilike(like),
            CapacityForecast.subject.ilike(like), Host.group_name.ilike(like),
            Host.source_instance.ilike(like),
        ))
    stmt = stmt.order_by(Host.hostname, CapacityForecast.metric_kind, CapacityForecast.subject).limit(limit)
    out = [_catalog_item(host, fc, fc.metric_kind, fc.subject) for fc, host in db.execute(stmt)]

    if kind in ("all", "cpu") and len(out) < limit:
        hstmt = select(Host).where(Host.cpu_pct.is_not(None))
        if host_id is not None:
            hstmt = hstmt.where(Host.id == host_id)
        if q:
            like = f"%{q.strip()}%"
            hstmt = hstmt.where(or_(
                Host.hostname.ilike(like), Host.ip.ilike(like),
                Host.group_name.ilike(like), Host.source_instance.ilike(like),
            ))
        seen = {item["id"] for item in out}
        for host in db.scalars(hstmt.order_by(Host.hostname).limit(limit - len(out))).all():
            item = _catalog_item(host, None, "cpu", "")
            if item["id"] not in seen:
                out.append(item)
    return out


def _load_state(db: Session, key: str) -> tuple[State, Host]:
    host_id, kind, subject = parse_key(key)
    host = db.get(Host, host_id)
    if host is None:
        raise ValueError("That server is no longer in the inventory.")
    row = db.scalar(select(CapacityForecast).where(
        CapacityForecast.host_id == host_id,
        CapacityForecast.metric_kind == kind,
        CapacityForecast.subject == subject,
    ))
    if row is None:
        if kind == "cpu" and host.cpu_pct is not None:
            total = _host_total(host, "cpu", "")
            return State(float(host.cpu_pct), 0.0, total,
                         total * float(host.cpu_pct) / 100.0 if total else None,
                         "cpu", "", [], None), host
        raise ValueError(
            f"No forecast baseline for {host.hostname} {kind} {subject}. "
            "The forecast runs nightly and a few minutes after start - press "
            "'Recompute forecast' on the Forecasting tab, then try again."
        )
    if row.classification in BLOCKED:
        raise ValueError(
            f"Simulation blocked: this baseline is '{row.classification}'. "
            f"{row.reason or 'More reliable history is needed first.'}"
        )
    current = float(row.current_pct or 0.0)
    total = row.total_value or _host_total(host, kind, subject)
    used = total * current / 100.0 if total else None
    return State(current, float(row.slope_pct_per_day or 0.0), total, used,
                 kind, subject, list(row.points or []), row.r_squared), host


def _sync_from_used(state: State) -> None:
    if state.total and state.used is not None:
        state.used = max(0.0, min(state.total, state.used))
        state.current_pct = state.used / state.total * 100.0


def _sync_from_pct(state: State) -> None:
    state.current_pct = max(0.0, min(100.0, state.current_pct))
    if state.total:
        state.used = state.total * state.current_pct / 100.0


def _to_pct_per_day(state: State, amount: float, unit: str | None) -> float | None:
    """A daily rate as %/day. ``unit`` 'gb' converts through the series size;
    None when that size is unknown."""
    if unit == "gb":
        if not state.total:
            return None
        return amount / state.total * 100.0
    return amount


def _apply(state: State, op: Operation, warnings: list[str]) -> None:
    kind = op.kind
    amount = float(op.value or 0.0)
    label = op_label(kind)
    size_unit = _unit(state.kind)

    if kind in ("add_once", "free_once", "resize") and not state.total:
        warnings.append(
            f"'{label}' was skipped: this series reports percent only, its size in "
            f"{size_unit} is not known. Use 'Add utilization' (percentage points) instead."
        )
        return

    if kind == "add_once":
        state.used = (state.used or 0.0) + amount
        _sync_from_used(state)
    elif kind == "free_once":
        state.used = max(0.0, (state.used or 0.0) - amount)
        _sync_from_used(state)
    elif kind in ("add_daily", "remove_daily"):
        rate = _to_pct_per_day(state, amount, op.unit)
        if rate is None:
            warnings.append(
                f"'{label}' in {size_unit}/day needs the series size, which is unknown "
                "here - enter the rate as %/day instead."
            )
            return
        state.slope += rate if kind == "add_daily" else -rate
    elif kind == "growth_multiplier":
        state.slope *= float(op.factor or 1.0)
    elif kind == "resize":
        new_total = float(op.absolute_total) if op.absolute_total is not None else state.total + amount
        if new_total <= 0:
            raise ValueError("The resized capacity must be greater than zero.")
        state.total = new_total
        _sync_from_used(state)
    elif kind == "threshold":
        state.threshold = float(op.threshold or 90.0)
    elif kind == "add_utilization":
        state.current_pct += amount
        _sync_from_pct(state)
    elif kind == "utilization_multiplier":
        state.current_pct *= float(op.factor or 1.0)
        _sync_from_pct(state)
    elif kind == "horizon_date":
        target_day = op.horizon_date or date.today()
        days = max(0.0, float((target_day - datetime.now(timezone.utc).date()).days))
        state.current_pct += state.slope * days
        _sync_from_pct(state)
        warnings.append(
            f"'{label}': the series was advanced {days:.0f} day(s) at its trend and "
            "the trend then frozen, so the ETA reads from that date."
        )
    else:
        raise ValueError(f"Unsupported operation: {kind}")

    if state.kind == "cpu" and kind in ("add_utilization", "utilization_multiplier"):
        warnings.append("CPU behaves non-linearly under load; treat CPU results as indicative only.")


def _forward_points(current: float, slope: float, horizon_days: int) -> list[list[float]]:
    """``[[day, pct], ...]`` from today (day 0) to the horizon, clamped 0..100."""
    step = max(1, horizon_days // 60)
    days = list(range(0, horizon_days + 1, step))
    if days[-1] != horizon_days:
        days.append(horizon_days)
    return [[float(d), round(max(0.0, min(100.0, current + slope * d)), 2)] for d in days]


def _horizon(*projections: Projection) -> int:
    """Look far enough ahead to show every dated crossing, within reason."""
    horizon = DEFAULT_HORIZON_DAYS
    for p in projections:
        for eta in (p.days_to_threshold, p.days_to_full):
            if eta is not None and eta < MAX_HORIZON_DAYS:
                horizon = max(horizon, int(math.ceil(eta)) + 7)
    return min(MAX_CHART_DAYS, horizon)


def _projection(state: State, *, gated: bool = True) -> Projection:
    """``gated`` applies the forecast's R² rule, which withholds dates from a
    weakly fitted line. The baseline keeps it, so this page and the
    Forecasting row never disagree. A scenario drops it: once the user has
    typed the growth or the resize themselves, the date follows from *their*
    numbers, and hiding it would answer a different question than they asked
    (the weak fit is reported as a warning instead)."""
    result = compute_projection(
        state.current_pct, state.slope, state.total,
        state.r_squared if gated else None,
        threshold=state.threshold, points=state.points,
    )
    return Projection(
        current_pct=round(state.current_pct, 2),
        slope_pct_per_day=round(state.slope, 4),
        total_value=state.total,
        used_value=round(state.used, 2) if state.used is not None else None,
        unit=_unit(state.kind),
        threshold=state.threshold,
        days_to_threshold=result.days_to_threshold,
        days_to_full=result.days_to_full,
        classification=result.classification,
        series_points=result.series_points,
        trust="trusted" if state.trusted else "blocked",
    )


def _delta(after: float | None, before: float | None, digits: int) -> float | None:
    if after is None or before is None:
        return None
    return round(after - before, digits)


def simulate(db: Session, request: SimulationRequest) -> dict[str, Any]:
    """Apply the operations in order to a copy of the baseline and report
    both. Every referenced target (the main one plus ``targets``) is loaded
    up front so a move between two of them is a single consistent run."""
    keys = list(dict.fromkeys([request.target, *request.targets]))
    baseline: dict[str, State] = {}
    hosts: dict[str, Host] = {}
    for key in keys:
        baseline[key], hosts[key] = _load_state(db, key)
    scenario = {key: replace(state, points=list(state.points)) for key, state in baseline.items()}
    warnings: list[str] = ["Simulation - a straight-line projection of the current trend, not a prediction."]

    for op in request.ops:
        if op.kind == "move_workload":
            src_key = op.source or request.target
            dst_key = op.destination or op.target
            if not dst_key or src_key not in scenario or dst_key not in scenario:
                raise ValueError("Moving a workload needs a destination target; pick one from the list.")
            if src_key == dst_key:
                raise ValueError("Source and destination of a move must differ.")
            src, dst = scenario[src_key], scenario[dst_key]
            size = float(op.size_gb if op.size_gb is not None else (op.value or 0.0))
            daily_gb = float(op.daily_gb or 0.0)
            if size:
                _apply(src, Operation(kind="free_once", value=size), warnings)
                _apply(dst, Operation(kind="add_once", value=size), warnings)
            if daily_gb:
                _apply(src, Operation(kind="remove_daily", value=daily_gb, unit="gb"), warnings)
                _apply(dst, Operation(kind="add_daily", value=daily_gb, unit="gb"), warnings)
            warnings.append(
                f"Move: {size:g} {_unit(src.kind)} and {daily_gb:g} {_unit(src.kind)}/day taken from "
                f"{hosts[src_key].hostname} and added to {hosts[dst_key].hostname}."
            )
        elif op.kind == "redistribute":
            src_key = op.source or request.target
            src = scenario.get(src_key)
            if src is None or not op.destinations:
                raise ValueError("Redistribution needs a source and at least one destination.")
            if abs(sum(float(d.get("percent", 0)) for d in op.destinations) - 100) > 0.01:
                raise ValueError("Redistribution percentages must add up to 100.")
            used, slope = src.used or 0.0, src.slope
            src.used, src.current_pct, src.slope = 0.0, 0.0, 0.0
            for item in op.destinations:
                dst = scenario.get(str(item.get("target", "")))
                if dst is None:
                    raise ValueError("Every redistribution destination must be in targets[].")
                share = float(item.get("percent", 0)) / 100.0
                _apply(dst, Operation(kind="add_once", value=used * share), warnings)
                _apply(dst, Operation(kind="add_daily", value=slope * share), warnings)
            warnings.append("Redistribution is linear: the load is assumed to move as-is.")
        else:
            _apply(scenario[request.target], op, warnings)

    base_p = _projection(baseline[request.target])
    scen_p = _projection(scenario[request.target], gated=False)
    horizon = _horizon(base_p, scen_p)
    base_p.forecast_points = _forward_points(base_p.current_pct, base_p.slope_pct_per_day, horizon)
    scen_p.forecast_points = _forward_points(scen_p.current_pct, scen_p.slope_pct_per_day, horizon)

    per_target = []
    for key in keys:
        b, s = _projection(baseline[key]), _projection(scenario[key], gated=False)
        per_target.append({
            "target": key, "hostname": hosts[key].hostname,
            "kind": baseline[key].kind, "subject": baseline[key].subject,
            "baseline": b.model_dump(), "scenario": s.model_dump(),
            "days_bought": _delta(s.days_to_threshold, b.days_to_threshold, 1),
        })

    if any(op.kind in ("resize", "add_once", "free_once", "move_workload") for op in request.ops):
        warnings.append("Used space is kept between 0 and the total capacity.")
    min_r2 = get_settings().forecast_min_r_squared
    r2 = baseline[request.target].r_squared
    if r2 is not None and r2 < min_r2 and request.ops:
        warnings.append(
            f"The measured trend for this series is weak (fit R² {r2:.2f}, below {min_r2:g}), which is "
            "why the Forecasting tab shows no date for it. The scenario date above rests on the "
            "numbers you entered more than on measured history."
        )

    host = hosts[request.target]
    kind, subject = baseline[request.target].kind, baseline[request.target].subject
    return {
        "target": {
            "id": request.target, "host_id": host.id, "hostname": host.hostname, "ip": host.ip,
            "kind": kind, "subject": subject, "instance": host.source_instance,
            "unit": _unit(kind),
        },
        "baseline": base_p.model_dump(),
        "scenario": scen_p.model_dump(),
        "deltas": {
            "days_bought": _delta(scen_p.days_to_threshold, base_p.days_to_threshold, 1),
            "days_to_threshold": _delta(scen_p.days_to_threshold, base_p.days_to_threshold, 1),
            "days_to_full": _delta(scen_p.days_to_full, base_p.days_to_full, 1),
            "current_pct": round(scen_p.current_pct - base_p.current_pct, 2),
            "slope_pct_per_day": round(scen_p.slope_pct_per_day - base_p.slope_pct_per_day, 4),
        },
        "horizon_days": horizon,
        "per_target": per_target,
        "warnings": list(dict.fromkeys(warnings)),
        "settings": {"min_r_squared": get_settings().forecast_min_r_squared},
    }
