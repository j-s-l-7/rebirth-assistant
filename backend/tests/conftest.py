import os
import tempfile
from pathlib import Path

# Isolated SQLite database for the test run; must be set before app modules import.
_TMP = Path(tempfile.mkdtemp(prefix="ra-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["COPILOT_PROVIDER"] = "anthropic"  # tests drive the Claude path with a fake client

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from tests.synthetic import seed_market  # noqa: E402


@pytest.fixture(scope="session")
def market():
    init_db()
    db = SessionLocal()
    try:
        return seed_market(db)
    finally:
        db.close()


@pytest.fixture
def db(market):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
