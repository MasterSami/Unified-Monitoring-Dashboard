from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.export_xlsx import build_workbook
from app.models import WhatIfScenario
from app.whatif.engine import simulate, target_catalog
from app.whatif.schemas import ScenarioDefinition, SimulationRequest

_PAGE = APIRouter(tags=["whatif"])
_API = APIRouter(prefix="/api/v1/whatif", tags=["whatif"])
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@_PAGE.get("/whatif", response_class=HTMLResponse)
def whatif_page(request: Request, settings: Settings = Depends(get_settings)):
    return _TEMPLATES.TemplateResponse(request, "whatif.html", {"request": request, "active_page": "whatif", "settings": settings})


@_API.get("/targets")
def targets(q: str = "", kind: str = "all", db: Session = Depends(get_db)):
    return {"targets": target_catalog(db, q, kind)}


@_API.post("/simulate")
def simulate_api(payload: SimulationRequest, db: Session = Depends(get_db)):
    try:
        return simulate(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@_API.get("/scenarios")
def list_scenarios(db: Session = Depends(get_db)):
    rows = db.scalars(select(WhatIfScenario).order_by(WhatIfScenario.updated_at.desc())).all()
    return [{"id": r.id, "name": r.name, "description": r.description, "target": r.target_json, "ops": r.ops_json, "updated_at": r.updated_at} for r in rows]


@_API.post("/scenarios")
def save_scenario(payload: ScenarioDefinition, db: Session = Depends(get_db)):
    row = WhatIfScenario(name=payload.name, description=payload.description, target_json={"id": payload.target}, ops_json=[op.model_dump(mode="json") for op in payload.ops], schema_version=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "target": row.target_json, "ops": row.ops_json}


@_API.put("/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, payload: ScenarioDefinition, db: Session = Depends(get_db)):
    row = db.get(WhatIfScenario, scenario_id)
    if row is None:
        raise HTTPException(404, "scenario not found")
    row.name, row.description = payload.name, payload.description
    row.target_json, row.ops_json = {"id": payload.target}, [op.model_dump(mode="json") for op in payload.ops]
    db.commit()
    return {"id": row.id, "name": row.name, "target": row.target_json, "ops": row.ops_json}


@_API.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int, db: Session = Depends(get_db)):
    row = db.get(WhatIfScenario, scenario_id)
    if row is None:
        raise HTTPException(404, "scenario not found")
    db.delete(row)
    db.commit()
    return {"deleted": scenario_id}


@_API.get("/export")
def export_scenario(scenario_id: int, db: Session = Depends(get_db)):
    row = db.get(WhatIfScenario, scenario_id)
    if row is None:
        raise HTTPException(404, "scenario not found")
    payload = SimulationRequest(target=row.target_json.get("id", ""), ops=row.ops_json)
    try:
        result = simulate(db, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    assumptions = [[f"Assumption {i}", op.get("kind", "")] for i, op in enumerate(row.ops_json, 1)]
    assumptions.append(["Important", "Simulation — linear projection, not a prediction."])
    columns = ["Metric", "Baseline", "Scenario", "Delta"]
    b, s, d = result["baseline"], result["scenario"], result["deltas"]
    rows = [("Current %", b["current_pct"], s["current_pct"], d.get("current_pct")), ("Slope %/day", b["slope_pct_per_day"], s["slope_pct_per_day"], d.get("slope_pct_per_day")), ("Days to threshold", b["days_to_threshold"], s["days_to_threshold"], d.get("days_to_threshold")), ("Classification", b["classification"], s["classification"], "")]
    data = build_workbook(sheet_title="What-If Simulation", period="Current baseline", filters_summary=row.name, columns=columns, rows=rows, credit="Simulation — linear projection, not a prediction.")
    wb = load_workbook(io.BytesIO(data))
    ws = wb.active
    insert_at = 7 if ws["A7"].value == "Metric" else 6
    ws.insert_rows(insert_at, len(assumptions) + 1)
    ws.cell(insert_at, 1, "ASSUMPTIONS").font = ws["A1"].font
    for idx, (label, value) in enumerate(assumptions, insert_at + 1):
        ws.cell(idx, 1, label); ws.cell(idx, 2, value)
    out = io.BytesIO(); wb.save(out)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(content=out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="SAMIX_whatif_{stamp}.xlsx"'})


page_router = _PAGE
api_router = _API
