"""Test isolation.

The suite used to run against whatever ``DATABASE_URL`` pointed at, so a
developer database that had been clicked through — a profile reserved, a
submission advanced — made assertions like "Rahul is selectable" fail for
reasons that had nothing to do with the code. Tests now get their own
throwaway SQLite file, seeded fresh, and the deterministic offline LLM.

Every environment variable is set *before* ``app.config`` is imported, because
that module reads configuration once at import time. ``load_dotenv`` does not
override variables that already exist, so these win over ``.env``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jdresume-tests-"))

os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["FILE_STORAGE_ROOT"] = str(_TMP / "storage")
os.environ["LLM_OFFLINE_MODE"] = "true"
os.environ["RESERVATION_SWEEPER_ENABLED"] = "false"
os.environ["CELERY_ENABLED"] = "false"
os.environ["CELERY_BROKER_URL"] = ""

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def seeded_database() -> None:
    """Create and seed the throwaway database once for the whole session."""
    from app.config.database import Base, engine
    from app.model_registry import import_all_models

    import_all_models()
    Base.metadata.create_all(engine)

    from scripts.seed import seed

    seed()
