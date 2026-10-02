from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai_poc.gateway import AIGateway, AIUserContext, DynatraceProblemRequest
from app.audit import record_audit
from app.config import Settings, get_settings
from app.db import get_db
from app.runbook_auth import COOKIE_NAME, read_token
from app.scheduler import get_service

logger = logging.getLogger("samix.ai_poc")
router = APIRouter(prefix="/api/v1/ai-poc", tags=["ai-poc"])


class DynatraceProblemQuery(BaseModel):
    instance: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    time_window_minutes: int = Field(default=60, ge=1, le=10080)
    severity_filter: str | None = Field(default=None, max_length=32)
    max_results: int = Field(default=50, ge=1, le=500)


def _operator(request: Request, settings: Settings) -> str:
    if not settings.enable_ai_poc:
        raise HTTPException(status_code=503, detail="AI PoC is disabled; set ENABLE_AI_POC=true")
    user = read_token(settings, request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="sign in via /runbook first")
    return user


@router.post("/dynatrace/problems")
def query_dynatrace_problems(
    payload: DynatraceProblemQuery,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Authenticated, read-only PoC route; no LLM is called yet."""
    user_id = _operator(request, settings)
    collector = get_service().get(payload.instance)
    if collector is None or getattr(collector, "name", "") != "dynatrace":
        raise HTTPException(status_code=404, detail="unknown Dynatrace instance")

    def audit_sink(event) -> None:
        record_audit(
            db,
            actor=event.actor,
            action=event.action,
            target=event.target,
            details={"tool": event.tool_name, "allowed": event.allowed, "reason": event.reason, "source": "ai-poc"},
        )

    gateway = AIGateway(enabled=True, audit_sink=audit_sink)
    result = gateway.get_dynatrace_problems(
        AIUserContext(user_id=user_id, authenticated=True, scopes=frozenset({"ai.read"})),
        DynatraceProblemRequest(
            hostname=payload.hostname,
            time_window_minutes=payload.time_window_minutes,
            severity_filter=payload.severity_filter,
            max_results=payload.max_results,
        ),
        collector,
    )
    db.commit()
    if not result.ok:
        raise HTTPException(status_code=503, detail={"code": result.error_code, "message": result.error_message})
    return result.to_model_context()
