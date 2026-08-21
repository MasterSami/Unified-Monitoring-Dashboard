"""Pytest fixtures: a fresh temp-DB app with the SiteScope ingest token set.

Environment is configured BEFORE importing the app so the module-level engine
binds to the throwaway SQLite file. The scheduler lifespan is not triggered
(TestClient is used without its context manager), so no background polling runs.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

INGEST_TOKEN = "test-token-123"
RUNBOOK_USER = "admin"
RUNBOOK_PASSWORD = "s3cret-pw"

# Configure the environment at IMPORT time — conftest is imported before any
# test module is collected, so the app's module-level engine binds to this
# throwaway DB no matter which test file imports app.db first. (Setting these
# inside the fixture is too late: a test module that imports app at collection
# time would otherwise bind the engine to the default ./dashboard.db and leak
# it into the repo, persisting across runs.)
_TMP_DB = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["MOCK_MODE"] = "false"
os.environ["ENABLED_COLLECTORS"] = ""  # no pull collectors in tests
os.environ["SITESCOPE_INGEST_TOKEN"] = INGEST_TOKEN

# Runbook: on, with one admin whose password is stored only as a digest.
os.environ["ENABLE_RUNBOOK"] = "true"
os.environ["RUNBOOK_USERS"] = (
    f"{RUNBOOK_USER}={hashlib.sha256(RUNBOOK_PASSWORD.encode()).hexdigest()}"
)
os.environ["RUNBOOK_SECRET"] = "test-signing-key"


@pytest.fixture(scope="session")
def client():
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import init_db

    init_db()
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)  # no `with` -> lifespan/scheduler not started
