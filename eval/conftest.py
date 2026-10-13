"""Isolation for the LLM-judged eval suite.

Deliberately its own top-level directory, not ``tests/eval`` — ``pyproject.toml``
scopes ``pytest``'s default ``testpaths`` to ``tests/`` alone, so a plain
``pytest`` run stays fast, deterministic and free, and never collects this
suite by accident. These tests call a real LLM twice over — once to generate
the resume under test, once as the judge — so they are slow and cost money on
purpose. Run them explicitly: ``pytest eval/``.

Mirrors ``tests/conftest.py``'s throwaway-database pattern, with one
deliberate difference: ``LLM_OFFLINE_MODE`` is never forced here. An eval suite
that judges the offline stub's canned text would be evaluating nothing.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jdresume-eval-"))

# ASSIGNED, not setdefault -- and that distinction is the whole reason this
# suite ran against the wrong database for as long as it existed.
#
# pytest imports its plugins before any conftest, and `deepeval` calls
# load_dotenv() at import time. So by the time this module runs, .env is
# ALREADY in os.environ -- including DATABASE_URL, which on a developer
# machine points at their real Postgres. setdefault() then quietly did
# nothing, and the eval reserved profiles and wrote requirements against
# live data instead of a throwaway file. It also could not pass: it picks
# `PersonProfile.first()` and reserves them, so any real person legitimately
# mid-composition failed the run with "Profile is EDITING for this vendor".
#
# Assignment wins over .env in the other direction too: app.config's own
# load_dotenv() does not override variables that already exist, so these
# values survive into the app under test. Same reasoning as
# tests/conftest.py, which got this right.
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'eval.db').as_posix()}"
os.environ["FILE_STORAGE_ROOT"] = str(_TMP / "storage")
os.environ["RESERVATION_SWEEPER_ENABLED"] = "false"
os.environ["CELERY_ENABLED"] = "false"
os.environ["CELERY_BROKER_URL"] = ""

# Deliberately NOT forced -- unlike the database, the LLM settings must come
# through from .env exactly as configured, or there is no real model to judge
# (see _llm_reachable below and this module's docstring).

import pytest  # noqa: E402


def _llm_reachable() -> bool:
    """False when there is nothing real for the judge to call.

    Checked before seeding rather than left to fail mid-test, so a machine
    with no key configured gets one clear skip reason instead of a stack of
    provider errors from every test in the file.
    """
    from app.config import LLM_OFFLINE_MODE
    from app.services.llm.factory import get_llm_provider

    if LLM_OFFLINE_MODE:
        return False
    try:
        get_llm_provider()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session", autouse=True)
def seeded_database() -> None:
    if not _llm_reachable():
        pytest.skip(
            "No live LLM provider configured (LLM_OFFLINE_MODE is on, or the "
            "configured LLM_PROVIDER has no usable API key) — this suite judges "
            "real model output, so it has nothing to run against."
        )

    from app.config.database import Base, engine
    from app.model_registry import import_all_models

    import_all_models()
    Base.metadata.create_all(engine)

    from scripts.seed import seed

    seed()
