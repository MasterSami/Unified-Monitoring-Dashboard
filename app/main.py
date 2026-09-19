"""FastAPI application: startup, scheduler wiring, and route mounting."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.routers import api, pages, runbook
from app.scheduler import (
    get_service,
    shutdown_scheduler,
    start_scheduler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB, seed first run on empty DB, and start the scheduler."""
    settings = get_settings()
    logger.info(
        "starting Unified Monitoring Dashboard (mock_mode=%s, collectors=%s)",
        settings.mock_mode,
        settings.enabled_collectors_list,
    )
    init_db()

    # A brand-new database gets the default correlation rules so the first
    # poll can already form incidents; a database that has any rule at all
    # (edited, disabled, or custom) is left exactly as the operator set it.
    from app.correlation_rules import ensure_default_rules
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        seeded = ensure_default_rules(db)
        db.commit()
        if seeded:
            logger.info("seeded %d default correlation rule(s)", seeded)
    finally:
        db.close()

    # Build collectors and start the scheduler. The scheduler fires an initial
    # run immediately (in the background) on every startup — so fresh data is
    # collected whether or not the DB already has rows — then repeats on the
    # configured interval. Running in the scheduler thread keeps startup fast
    # even when some instances are slow or unreachable.
    get_service()
    start_scheduler(settings)
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="Unified Monitoring Dashboard",
    description="Aggregates Zabbix, Dynatrace, and NNMi into one view.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(pages.router)
app.include_router(api.router)
app.include_router(runbook.router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
