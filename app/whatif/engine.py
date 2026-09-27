from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.forecast import compute_projection
from app.models import CapacityForecast, Host
from app.whatif.schemas import Operation, Projection, SimulationRequest


@dataclass
class State:
    current_pct: float
    slope: float
    total: float | None
    used: float | None
    kind: str
    subject: str
    points: list[list[float]]
    r_squared: float | None
    threshold: float = 90.0


def target_key(host_id: int, kind: str, subject: str = "") -> str:
    return f"{host_id}:{kind}:{subject}"


def parse_key(value: str) -> tuple[int, str, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or not parts[0].isdigit():
        raise ValueError("target must be a valid capacity target")
    return int(parts[0]), parts[1], parts[2]


def _target_row(db: Session, key: str):
    host_id, kind, subject = parse_key(key)
    row = db.scalar(select(CapacityForecast).where(
        CapacityForecast.host_id == host_id,
        CapacityForecast.metric_kind == kind,
        CapacityForecast.subject == subject,
    ))
    host = db.get(Host, host_id)
    if host is None:
        raise ValueError("target host no longer exists")
    return row, host, kind, subject


def target_catalog(db: Session, q: str = "", kind: str = "all", limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(CapacityForecast, Host).join(Host, Host.id == CapacityForecast.host_id)
    if kind != "all":
        stmt = stmt.where(CapacityForecast.metric_kind == kind)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Host.hostname.ilike(like), Host.ip.ilike(like), CapacityForecast.subject.ilike(like), Host.group_name.ilike(like)))
    out = []
    for forecast, host in db.execute(stmt.order_by(Host.hostname, CapacityForecast.metric_kind, CapacityForecast.subject).limit(limit)):
        trusted = forecast.classification not in {"noisy", "insufficient_data"} and forecast.r_squared is not None
        out.append({
            "id": target_key(host.id, forecast.metric_kind, forecast.subject),
            "label": f"{host.hostname} · {forecast.metric_kind}{(' · ' + forecast.subject) if forecast.subject else ''}",
            "hostname": host.hostname, "ip": host.ip, "platform": forecast.platform,
            "instance": host.source_instance, "group": host.group_name or "", "kind": forecast.metric_kind,
            "subject": forecast.subject, "current_pct": forecast.current_pct, "slope_pct_per_day": forecast.slope_pct_per_day,
            "r_squared": forecast.r_squared, "days_to_threshold": forecast.days_to_threshold_90,
            "classification": forecast.classification, "trust": "trusted" if trusted else "blocked",
            "reason": forecast.reason,
        })
    # CPU is intentionally not forecast as days-to-full, but it is still a
    # useful What-If target for sustained utilization scenarios.
    if kind in ("all", "cpu") and len(out) < limit:
        hosts = db.scalars(select(Host).where(Host.cpu_pct.is_not(None)).order_by(Host.hostname).limit(limit)).all()
        for host in hosts:
            key = target_key(host.id, "cpu", "")
            if any(item["id"] == key for item in out):
                continue
            out.append({
                "id": key, "label": f"{host.hostname} · cpu", "hostname": host.hostname,
                "ip": host.ip, "platform": host.source_platform.value, "instance": host.source_instance,
                "group": host.group_name or "", "kind": "cpu", "subject": "",
                "current_pct": host.cpu_pct, "slope_pct_per_day": 0.0, "r_squared": None,
                "days_to_threshold": None, "classification": "ok", "trust": "trusted",
                "reason": "CPU is indicative only; no days-to-full forecast is produced.",
            })
    return out


def _state(row: CapacityForecast, kind: str, subject: str) -> State:
    current = float(row.current_pct or 0.0)
    total = float(row.total_value) if row.total_value is not None else None
    used = total * current / 100.0 if total is not None else None
    return State(current, float(row.slope_pct_per_day or 0.0), total, used, kind, subject, list(row.points or []), row.r_squared)


def _projection(state: State) -> Projection:
    result = compute_projection(state.current_pct, state.slope, state.total, state.r_squared, threshold=state.threshold, points=state.points)
    return Projection(
        current_pct=round(state.current_pct, 2), slope_pct_per_day=round(state.slope, 4),
        total_value=state.total, used_value=state.used, threshold=state.threshold,
        days_to_threshold=result.days_to_threshold, days_to_full=result.days_to_full,
        classification=result.classification, series_points=result.series_points,
        trust="trusted" if state.r_squared is None or state.r_squared >= 0.5 else "blocked",
    )


def _recalc(state: State) -> None:
    if state.total is not None and state.total > 0 and state.used is not None:
        state.used = max(0.0, min(state.total, state.used))
        state.current_pct = max(0.0, min(100.0, state.used / state.total * 100.0))


def _apply(state: State, op: Operation, states: dict[str, State], warnings: list[str]) -> None:
    kind = op.kind
    amount = float(op.value or 0.0)
    if kind == "add_once": state.used = (state.used or 0) + amount; _recalc(state)
    elif kind == "free_once": state.used = max(0.0, (state.used or 0) - amount); _recalc(state)
    elif kind == "add_daily": state.slope += amount
    elif kind == "remove_daily": state.slope -= amount
    elif kind == "growth_multiplier": state.slope *= float(op.factor or 1.0)
    elif kind == "resize":
        old = state.total
        new_total = float(op.absolute_total) if op.absolute_total is not None else (old or 0) + amount
        if new_total <= 0: raise ValueError("resize total must be greater than zero")
        state.total = new_total; _recalc(state)
    elif kind == "threshold": state.threshold = float(op.threshold or 90.0)
    elif kind == "add_utilization": state.current_pct += amount; state.used = None
    elif kind == "utilization_multiplier": state.current_pct *= float(op.factor or 1.0); state.used = None
    elif kind == "horizon_date":
        days = max(0.0, ((op.horizon_date or date.today()) - datetime.now(timezone.utc).date()).days)
        state.current_pct = max(0.0, min(100.0, state.current_pct + state.slope * days)); state.slope = 0.0; state.used = None
    elif kind in {"move_workload", "redistribute", "scope_service"}:
        warnings.append(f"{kind.replace('_', ' ').title()} is represented in the per-target result; apply it with explicit target operations for exact sizing.")
    else:
        raise ValueError(f"unsupported operation: {kind}")
    if state.kind == "cpu" and kind in {"add_utilization", "utilization_multiplier"}:
        warnings.append("CPU behaves non-linearly under load; indicative only.")


def simulate(db: Session, request: SimulationRequest) -> dict[str, Any]:
    def load_state(key: str) -> tuple[State, Host, str, str]:
        row, host, kind, subject = _target_row(db, key)
        if row is None and kind == "cpu" and subject == "" and host.cpu_pct is not None:
            return State(float(host.cpu_pct), 0.0, None, None, kind, subject, [], None), host, kind, subject
        if row is None:
            raise ValueError("no forecast baseline exists for this target")
        if row.classification in {"noisy", "insufficient_data"}:
            raise ValueError(f"Simulation blocked: baseline is {row.classification}. {row.reason or 'More reliable history is required.'}")
        return _state(row, kind, subject), host, kind, subject

    keys = list(dict.fromkeys([request.target, *request.targets]))
    baseline_states: dict[str, State] = {}
    hosts: dict[str, Host] = {}
    kinds: dict[str, tuple[str, str]] = {}
    for key in keys:
        state, host, kind, subject = load_state(key)
        baseline_states[key] = state
        hosts[key] = host
        kinds[key] = (kind, subject)
    baseline_state = baseline_states[request.target]
    scenario_state = State(**baseline_state.__dict__)
    scenario_states = {key: State(**state.__dict__) for key, state in baseline_states.items()}
    warnings: list[str] = ["Simulation — linear projection, not a prediction."]
    for op in request.ops:
        if op.kind == "move_workload":
            source = scenario_states.get(op.source or request.target)
            destination = scenario_states.get(op.destination or op.target or request.target)
            if source is None or destination is None:
                raise ValueError("move_workload requires source and destination targets in targets[]")
            size = float(op.size_gb or op.value or 0)
            daily = float(op.daily_gb or 0)
            _apply(source, Operation(kind="free_once", value=size), scenario_states, warnings)
            _apply(source, Operation(kind="remove_daily", value=daily), scenario_states, warnings)
            _apply(destination, Operation(kind="add_once", value=size), scenario_states, warnings)
            _apply(destination, Operation(kind="add_daily", value=daily), scenario_states, warnings)
            warnings.append("Workload move applies the stated GB and GB/day components to both targets.")
        elif op.kind == "redistribute":
            source = scenario_states.get(op.source or request.target)
            if source is None or not op.destinations:
                raise ValueError("redistribute requires a source and destinations")
            total_split = sum(float(item.get("percent", 0)) for item in op.destinations)
            if abs(total_split - 100) > 0.01:
                raise ValueError("redistribute destination percentages must sum to 100")
            used, slope = source.used or 0.0, source.slope
            source.used, source.current_pct, source.slope = 0.0, 0.0, 0.0
            for item in op.destinations:
                dest = scenario_states.get(str(item.get("target", "")))
                if dest is None:
                    raise ValueError("redistribute destination is not present in targets[]")
                share = float(item.get("percent", 0)) / 100.0
                _apply(dest, Operation(kind="add_once", value=used * share), scenario_states, warnings)
                _apply(dest, Operation(kind="add_daily", value=slope * share), scenario_states, warnings)
            warnings.append("Linear redistribution — assumes load moves as-is.")
        else:
            _apply(scenario_states[request.target], op, scenario_states, warnings)
        scenario_state = scenario_states[request.target]
    baseline = _projection(baseline_state)
    scenario = _projection(scenario_state)
    if any(op.kind in {"resize", "add_once", "free_once"} for op in request.ops):
        warnings.append("Used values are clamped between 0 and total capacity.")
    return {
        "target": {"id": request.target, "hostname": host.hostname, "ip": host.ip, "kind": kind, "subject": subject, "instance": host.source_instance},
        "baseline": baseline.model_dump(), "scenario": scenario.model_dump(),
        "deltas": {
            "days_to_threshold": None if baseline.days_to_threshold is None or scenario.days_to_threshold is None else round(scenario.days_to_threshold - baseline.days_to_threshold, 1),
            "days_bought": None if baseline.days_to_threshold is None or scenario.days_to_threshold is None else round(scenario.days_to_threshold - baseline.days_to_threshold, 1),
            "current_pct": round(scenario.current_pct - baseline.current_pct, 2),
            "slope_pct_per_day": round(scenario.slope_pct_per_day - baseline.slope_pct_per_day, 4),
        }, "per_target": [
            {"target": key, "hostname": hosts[key].hostname, "baseline": _projection(baseline_states[key]).model_dump(), "scenario": _projection(scenario_states[key]).model_dump()}
            for key in scenario_states
        ], "warnings": list(dict.fromkeys(warnings)),
    }
