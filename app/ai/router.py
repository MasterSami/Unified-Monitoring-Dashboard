"""HTTP surface of SAMIX AI.

    GET  /ai                         the chat page (or the disabled / sign-in gate)
    POST /partials/ai/ask            HTMX: start a question, return the progress card
    GET  /partials/ai/progress/{id}  HTMX: poll; progress card until done, then the answer
    POST /api/v1/ai/ask              JSON: {question} -> full result (synchronous)
    POST /api/v1/ai/feedback         JSON or form: {trace_id, vote, comment}
    GET  /api/v1/ai/health           is Ollama up and the model present

Everything is gated by the gateway: AI_ENABLED off -> 404, no Runbook session
(when required) -> 401, rate limit -> 429.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.routers.pages import templates

from .audit import record_feedback
from .evidence import platform_label, render_answer_html
from .gateway import Gateway, GatewayError, get_gateway

router = APIRouter(tags=["ai"])

EXAMPLES = (
    "فيه مشاكل ايه على MW10؟",
    "Is web-01 monitored, and by which tools?",
    "أخطر 10 alerts شغالة دلوقتي",
    "Which disks reach 90% within 30 days?",
    "db-02 حالته ايه؟",
)


def _gate(request: Request, gw: Gateway) -> str:
    try:
        return gw.require_user(request)
    except GatewayError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _answer_context(result, user: str) -> dict:
    facts = []
    for f in result.pack.facts:
        facts.append({**f, "platform_label": platform_label(f.get("source_platform")),
                      "as_of_short": (f.get("as_of") or "")[11:16],
                      "summary": _fact_summary(f)})
    return {
        "r": result,
        "user": user,
        "answer_html": render_answer_html(result.answer),
        "facts": facts,
        "show_raw": not result.validated,
        "platform_label": platform_label,
    }


def _fact_summary(fact: dict) -> str:
    d = fact.get("data", {})
    if fact["tool"] == "get_host_alerts" or fact["tool"] == "get_active_alerts_summary":
        return f"[{d.get('severity_label') or d.get('severity')}] {d.get('host') or '-'}: {d.get('title') or ''} ({d.get('state')})"
    if fact["tool"] == "get_host_status":
        return f"{d.get('hostname')} is {d.get('status')} (group: {d.get('group') or '-'}, last seen {(d.get('last_seen') or '')[11:16] or '-'})"
    if fact["tool"] == "get_capacity_risk":
        eta = d.get("days_to_90pct")
        return (f"{d.get('hostname')} {d.get('resource')} {d.get('subject') or ''}: {d.get('current_pct')}% "
                f"-> {d.get('classification')}" + (f", 90% in {eta:g}d" if isinstance(eta, (int, float)) else ""))
    return json.dumps(d, ensure_ascii=False)[:160]


# --- page ----------------------------------------------------------------------


@router.get("/ai", response_class=HTMLResponse)
def ai_page(request: Request, settings: Settings = Depends(get_settings)):
    gw = get_gateway()
    if not gw.enabled:
        return templates.TemplateResponse(request, "ai_disabled.html", {"active_page": "ai"}, status_code=404)
    user = gw.identify(request)
    if user is None:
        return templates.TemplateResponse(
            request, "ai_disabled.html",
            {"active_page": "ai", "needs_login": True}, status_code=401,
        )
    health = gw.health()
    return templates.TemplateResponse(request, "ai.html", {
        "active_page": "ai", "user": user, "health": health, "examples": EXAMPLES,
        "rate_limit": settings.ai_rate_limit_per_min,
    })


# --- HTMX flow ---------------------------------------------------------------------


@router.post("/partials/ai/ask", response_class=HTMLResponse)
async def ai_ask_partial(request: Request):
    gw = get_gateway()
    try:
        user = gw.require_user(request)
        form = await request.form()
        question = gw.validate_question(str(form.get("question", "")))
        gw.limiter.check(user)
    except GatewayError as exc:
        return HTMLResponse(
            f"<div class='ai-msg ai-error notice rb-error'>{exc.detail}</div>", status_code=exc.status_code
        )
    job = gw.start_job(question, user)
    return templates.TemplateResponse(request, "partials/ai_progress.html", {"job": job, "poll": True})


@router.get("/partials/ai/progress/{trace_id}", response_class=HTMLResponse)
def ai_progress_partial(request: Request, trace_id: str):
    gw = get_gateway()
    _gate(request, gw)
    job = gw.job(trace_id)
    if job is None:
        return HTMLResponse("<div class='ai-msg ai-error notice rb-error'>That question has expired.</div>", 404)
    if not job.done:
        return templates.TemplateResponse(request, "partials/ai_progress.html", {"job": job, "poll": True})
    if job.error is not None:
        return HTMLResponse(f"<div class='ai-msg ai-error notice rb-error'>{job.error.detail}</div>")
    return templates.TemplateResponse(request, "partials/ai_answer.html", _answer_context(job.result, job.user))


# --- JSON API -----------------------------------------------------------------------


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


@router.post("/api/v1/ai/ask")
def ai_ask(payload: AskIn, request: Request):
    gw = get_gateway()
    user = _gate(request, gw)
    try:
        question = gw.validate_question(payload.question)
        gw.limiter.check(user)
    except GatewayError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    result = gw.ask(question, user)
    return JSONResponse(result.to_dict(), status_code=200 if not result.error else 502)


class FeedbackIn(BaseModel):
    trace_id: str = Field(min_length=4, max_length=64)
    vote: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/api/v1/ai/feedback")
async def ai_feedback(request: Request):
    gw = get_gateway()
    user = _gate(request, gw)
    if request.headers.get("content-type", "").startswith("application/json"):
        data = FeedbackIn.model_validate(await request.json())
    else:
        form = await request.form()
        data = FeedbackIn(trace_id=str(form.get("trace_id", "")), vote=int(form.get("vote", 0) or 0),
                          comment=str(form.get("comment") or "") or None)
    if data.vote == 0:
        raise HTTPException(422, "vote must be 1 or -1")
    if not record_feedback(trace_id=data.trace_id, vote=data.vote, comment=data.comment, user=user):
        raise HTTPException(404, "unknown trace_id")
    if request.headers.get("hx-request"):
        label = "Thanks, noted as helpful." if data.vote > 0 else "Thanks, noted. This goes into the evaluation set."
        return HTMLResponse(f"<span class='ai-voted'>{label}</span>")
    return {"ok": True, "trace_id": data.trace_id, "vote": data.vote}


@router.get("/api/v1/ai/health")
def ai_health(request: Request):
    gw = get_gateway()
    _gate(request, gw)
    return gw.health()
