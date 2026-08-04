"""Pytest fixtures: a fresh temp-DB app with the SiteScope ingest token set.

Environment is configured BEFORE importing the app so the module-level engine
binds to the throwaway SQLite file. The scheduler lifespan is not triggered
(TestClient is used without its context manager), so no background polling runs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

INGEST_TOKEN = "test-token-123"


@pytest.fixture(scope="session")
def client():
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}"
    os.environ["MOCK_MODE"] = "false"
    os.environ["ENABLED_COLLECTORS"] = ""  # no pull collectors in tests
    os.environ["SITESCOPE_INGEST_TOKEN"] = INGEST_TOKEN

    # Import only after the environment is set.
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import init_db

    init_db()
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)  # no `with` -> lifespan/scheduler not started
