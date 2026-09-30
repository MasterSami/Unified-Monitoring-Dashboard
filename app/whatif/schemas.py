from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Operation(BaseModel):
    """One step of a scenario, applied in order to the target's baseline."""

    kind: str
    #: Amount for size/rate/utilization steps (GB, %/day, or percentage points).
    value: float | None = None
    factor: float | None = None
    #: For daily rates: "gb" (converted through the series size) or "pct".
    unit: Literal["gb", "pct"] | None = None
    target: str | None = None
    source: str | None = None
    destination: str | None = None
    size_gb: float | None = Field(default=None, ge=0)
    daily_gb: float | None = Field(default=None, ge=0)
    destinations: list[dict[str, Any]] = Field(default_factory=list)
    threshold: float | None = Field(default=None, ge=50, le=99)
    horizon_date: date | None = None
    absolute_total: float | None = Field(default=None, gt=0)

    @field_validator("factor")
    @classmethod
    def factor_range(cls, value: float | None) -> float | None:
        if value is not None and not 0.1 <= value <= 10:
            raise ValueError("factor must be between 0.1 and 10")
        return value


class SimulationRequest(BaseModel):
    target: str
    targets: list[str] = Field(default_factory=list)
    ops: list[Operation] = Field(default_factory=list)


class ScenarioDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    target: str
    targets: list[str] = Field(default_factory=list)
    ops: list[Operation] = Field(default_factory=list)


class Projection(BaseModel):
    current_pct: float
    slope_pct_per_day: float
    total_value: float | None = None
    used_value: float | None = None
    unit: str = "GB"
    threshold: float = 90.0
    days_to_threshold: float | None = None
    days_to_full: float | None = None
    classification: str
    #: The daily points the forecast was fitted from (history, day offsets).
    series_points: list[list[float]] = Field(default_factory=list)
    #: The projection forward from today: [[days ahead, pct], ...].
    forecast_points: list[list[float]] = Field(default_factory=list)
    trust: str = "trusted"


class SimulationResponse(BaseModel):
    target: dict[str, Any]
    baseline: Projection
    scenario: Projection
    deltas: dict[str, Any]
    horizon_days: int = 90
    per_target: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
