"""
Points the whole test session at its own throwaway SQLite file before
any app module is imported, so tests never touch data/auditai.db (the
real local dev database).
"""

import os
import sys
from pathlib import Path

TEST_DB_PATH = Path(__file__).parent / "test_auditai.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(autouse=True, scope="session")
def _cleanup_test_db():
    yield
    # Best-effort: on Windows, SQLAlchemy's engine can still hold the file
    # open at session teardown, which turns a harmless cleanup step into a
    # PermissionError - not worth failing the run over. .gitignore already
    # excludes this file either way.
    try:
        from app.database.session import engine
        engine.dispose()
    except Exception:
        pass
    try:
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
    except PermissionError:
        pass
