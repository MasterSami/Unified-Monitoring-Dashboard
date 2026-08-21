"""Runbook routes: admin login, script catalogue, execution and export.

Everything here is gated twice — once on ``ENABLE_RUNBOOK`` (the feature flag,
mirroring Topology) and once on a signed admin session cookie. A run holds a
slot from a small semaphore so a burst of clicks cannot exhaust FastAPI's
threadpool, and its results are cached briefly so pressing Export does not
re-query the monitoring tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.export_xlsx import build_workbook
from app.routers.pages import templates
from app.runbook import (
    PLATFORMS,
    SCRIPTS_BY_SLUG,
    RunbookError,
    Script,
    execute,
    runbook_instances,
    scripts_for,
)
from app.runbook_auth import (
    COOKIE_NAME,
    cookie_max_age,
    is_configured,
    issue_token,
    read_token,
    verify_credentials,
)

logger = logging.getLogger("runbook.routes")

router = APIRouter(tags=["runbook"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: At most this many script runs execute at once. Each one holds a threadpool
#: thread while it waits on the monitoring API, so letting them pile up would
#: starve ordinary page requests.
_RUN_SLOTS = threading.BoundedSemaphore(2)
_RUN_WAIT_SECONDS = 20

#: Recent results, so Export reuses the run the user is looking at instead of
#: re-querying. Keyed by (user, slug, instance, params); small and short-lived.
_RESULT_TTL_SECONDS = 900
_RESULT_CACHE_MAX = 16
_results: dict[str, tuple[float, list[list]]] = {}
_results_lock = threading.Lock()


# --- Guards -----------------------------------------------------------------


def _feature_on(settings: Settings) -> bool:
    return bool(settings.enable_runbook)


def _disabled_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "runbook_disabled.html", {"active_page": "runbook"}, status_code=404
    )


def _login_page(
    request: Request, settings: Settings, error: str = "", status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "runbook_login.html",
        {
            "active_page": "runbook",
            "error": error,
            "configured": is_configured(settings),
        },
        status_code=status_code,
    )


# --- Result cache -----------------------------------------------------------


def _cache_key(user: str, slug: str, instance: str, params: dict[str, str]) -> str:
    blob = json.dumps(
        [user, slug, instance, sorted(params.items())], separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_put(key: str, rows: list[list]) -> None:
    now = time.time()
    with _results_lock:
        _results[key] = (now, rows)
        # Drop expired entries first, then the oldest if still over budget.
        for k in [k for k, (t, _) in _results.items() if now - t > _RESULT_TTL_SECONDS]:
            _results.pop(k, None)
        while len(_results) > _RESULT_CACHE_MAX:
            _results.pop(min(_results, key=lambda k: _results[k][0]), None)


def _cache_get(key: str) -> list[list] | None:
    with _results_lock:
        hit = _results.get(key)
        if hit is None:
            return None
        stamped, rows = hit
        if time.time() - stamped > _RESULT_TTL_SECONDS:
            _results.pop(key, None)
            return None
        return rows


# --- Request helpers --------------------------------------------------------


def _collect_params(script: Script, form: dict) -> dict[str, str]:
    """Pull just this script's declared params out of the submitted form."""
    return {p.name: str(form.get(p.name, "") or "").strip() for p in script.params}


def _run(script: Script, instance: str, params: dict[str, str], settings: Settings):
    """Execute a script under the concurrency guard."""
    if not _RUN_SLOTS.acquire(timeout=_RUN_WAIT_SECONDS):
        raise RunbookError(
            "Another Runbook script is still running. Give it a moment and try again."
        )
    try:
        started = time.monotonic()
        rows = execute(script, instance, params, settings)
        elapsed = time.monotonic() - started
        logger.info(
            "runbook %s on %s -> %d row(s) in %.1fs",
            script.slug, instance or "all", len(rows), elapsed,
        )
        return rows, elapsed
    finally:
        _RUN_SLOTS.release()


def _script_context(
    request: Request,
    script: Script,
    settings: Settings,
    *,
    instance: str = "all",
    params: dict[str, str] | None = None,
    rows: list[list] | None = None,
    elapsed: float | None = None,
    error: str = "",
    token: str = "",
) -> dict:
    return {
        "active_page": "runbook",
        "script": script,
        "instances": runbook_instances(settings, script.platform),
        "instance": instance,
        "params": params or {},
        "rows": rows,
        "elapsed": elapsed,
        "error": error,
        "token": token,
        "allow_write": settings.runbook_allow_write,
        "enable_export": settings.enable_export,
        "ran_at": datetime.now(timezone.utc),
    }


# --- Routes -----------------------------------------------------------------


@router.get("/runbook", response_class=HTMLResponse)
def runbook_page(
    request: Request,
    platform: str = "all",
    settings: Settings = Depends(get_settings),
):
    """The script catalogue, or the login form when not signed in."""
    if not _feature_on(settings):
        return _disabled_page(request)
    user = read_token(settings, request.cookies.get(COOKIE_NAME))
    if not user:
        return _login_page(request, settings)

    platform = platform if platform in PLATFORMS else "all"
    return templates.TemplateResponse(
        request,
        "runbook.html",
        {
            "active_page": "runbook",
            "user": user,
            "platform": platform,
            "platforms": PLATFORMS,
            "scripts": scripts_for(platform),
            "total": len(scripts_for("all")),
        },
    )


@router.post("/runbook/login", response_class=HTMLResponse)
def runbook_login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    """Check credentials and issue the signed session cookie."""
    if not _feature_on(settings):
        return _disabled_page(request)
    if not is_configured(settings):
        return _login_page(
            request, settings,
            "No Runbook admin is configured. Set RUNBOOK_USERS in .env.",
            status_code=503,
        )

    user = verify_credentials(settings, username, password)
    if user is None:
        logger.warning("runbook login rejected for %r", (username or "")[:40])
        return _login_page(request, settings, "Wrong username or password.", 401)

    logger.info("runbook login: %s", user)
    response = RedirectResponse("/runbook", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        issue_token(settings, user),
        max_age=cookie_max_age(settings),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/runbook/logout")
def runbook_logout() -> RedirectResponse:
    """Clear the session cookie."""
    response = RedirectResponse("/runbook", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/runbook/{slug}", response_class=HTMLResponse)
def runbook_script_page(
    request: Request,
    slug: str,
    instance: str = "all",
    settings: Settings = Depends(get_settings),
):
    """One script's documentation, with its Run form."""
    if not _feature_on(settings):
        return _disabled_page(request)
    if not read_token(settings, request.cookies.get(COOKIE_NAME)):
        return _login_page(request, settings)

    script = SCRIPTS_BY_SLUG.get(slug)
    if script is None:
        return templates.TemplateResponse(
            request, "runbook_missing.html",
            {"active_page": "runbook", "slug": slug}, status_code=404,
        )
    return templates.TemplateResponse(
        request, "runbook_script.html",
        _script_context(request, script, settings, instance=instance),
    )


@router.post("/partials/runbook/run/{slug}", response_class=HTMLResponse)
async def runbook_run(
    request: Request,
    slug: str,
    settings: Settings = Depends(get_settings),
):
    """Execute a script and swap in its results table (HTMX)."""
    if not _feature_on(settings):
        return HTMLResponse("<div class='notice'>Runbook is disabled.</div>", 404)
    user = read_token(settings, request.cookies.get(COOKIE_NAME))
    if not user:
        return HTMLResponse(
            "<div class='notice'>Your session expired. "
            "<a href='/runbook'>Sign in again</a>.</div>",
            status_code=401,
        )

    script = SCRIPTS_BY_SLUG.get(slug)
    if script is None:
        return HTMLResponse("<div class='notice'>Unknown script.</div>", 404)

    form = dict(await request.form())
    instance = str(form.get("instance", "all") or "all")
    params = _collect_params(script, form)

    try:
        rows, elapsed = _run(script, instance, params, settings)
    except RunbookError as exc:
        return templates.TemplateResponse(
            request, "partials/runbook_results.html",
            _script_context(request, script, settings, instance=instance,
                            params=params, error=str(exc)),
        )
    except Exception as exc:  # noqa: BLE001 — surface, never 500 the panel
        logger.exception("runbook %s failed", slug)
        return templates.TemplateResponse(
            request, "partials/runbook_results.html",
            _script_context(request, script, settings, instance=instance,
                            params=params,
                            error=f"{type(exc).__name__}: {exc}"),
        )

    token = _cache_key(user, slug, instance, params)
    _cache_put(token, rows)
    return templates.TemplateResponse(
        request, "partials/runbook_results.html",
        _script_context(request, script, settings, instance=instance, params=params,
                        rows=rows, elapsed=elapsed, token=token),
    )


@router.get("/runbook/{slug}/export.xlsx")
def runbook_export(
    request: Request,
    slug: str,
    instance: str = "all",
    token: str = "",
    settings: Settings = Depends(get_settings),
) -> Response:
    """Download the last run of this script as a branded workbook."""
    if not _feature_on(settings) or not settings.enable_export:
        return Response("Export is disabled.", status_code=404)
    user = read_token(settings, request.cookies.get(COOKIE_NAME))
    if not user:
        return Response("Sign in to the Runbook first.", status_code=401)

    script = SCRIPTS_BY_SLUG.get(slug)
    if script is None:
        return Response("Unknown script.", status_code=404)

    rows = _cache_get(token) if token else None
    if rows is None:
        # The cached run expired (or the link was opened later) — re-run it with
        # the same inputs rather than handing back an empty sheet.
        params = {k: v for k, v in request.query_params.items()
                  if k in {p.name for p in script.params}}
        try:
            rows, _ = _run(script, instance, params, settings)
        except RunbookError as exc:
            return Response(str(exc), status_code=400)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    data = build_workbook(
        sheet_title=script.title[:31],
        period=f"Run at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        filters_summary=f"{script.platform} · instance: {instance}",
        columns=script.columns,
        rows=rows,
        credit=f"Script by {script.author}",
    )
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition":
                f'attachment; filename="SAMIX_{script.slug}_{stamp}.xlsx"'
        },
    )
