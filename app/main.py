"""FastAPI application: startup, scheduler wiring, and route mounting."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.routers import api, pages
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

    service = get_service()
    # On first startup with an empty DB, run one collection immediately so the
    # UI has data to show before the first scheduled interval elapses.
    if not service.has_data():
        logger.info("empty database detected; running initial collection")
        service.run_all()

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


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
