from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="invoice-match-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TEST_DIRECTORY.name) / 'test.db'}"
os.environ["INVOICE_MATCH_RULES"] = str(ROOT / "config" / "rules.yaml")
os.environ["REVIEWER_TOKENS"] = json.dumps({
    "finance-reviewer": {
        "token": "test-reviewer-secret-token-for-unit-tests-0001",
        "roles": ["reviewer"],
    },
    "finance-manager": {
        "token": "test-reviewer-secret-token-for-unit-tests-0002",
        "roles": ["reviewer", "approver"],
    },
    "cfo": {
        "token": "test-reviewer-secret-token-for-unit-tests-0003",
        "roles": ["reviewer", "approver", "admin"],
    },
})
os.environ["INTEGRATION_TOKENS"] = json.dumps({
    "ap-import": "test-integration-secret-token-for-unit-tests-0001",
})

from invoice_match.database import engine  # noqa: E402
from invoice_match.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def apply_database_migrations():
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    yield
    _TEST_DIRECTORY.cleanup()


@pytest.fixture(autouse=True)
def clear_database(apply_database_migrations):
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM document_capture_events")
        connection.exec_driver_sql("DELETE FROM captured_documents")
        connection.exec_driver_sql("DELETE FROM ap_outbox")
        connection.exec_driver_sql("DELETE FROM audit_events")
        connection.exec_driver_sql("DELETE FROM exceptions")
        connection.exec_driver_sql("DELETE FROM matches")


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def reviewer_headers():
    return {"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0002"}


@pytest.fixture
def viewer_headers():
    return {"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0001"}


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0003"}


@pytest.fixture
def integration_headers():
    return {"Authorization": "Bearer test-integration-secret-token-for-unit-tests-0001"}
