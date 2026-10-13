"""Import every model module so ``Base.metadata`` is complete.

Used by Alembic's env.py and by the dev/seed scripts. Add a line here whenever
a new ``app.models.<module>`` is created.
"""

from __future__ import annotations

import importlib

_MODEL_MODULES: tuple[str, ...] = (
    "app.models.user",
    "app.models.company",
    "app.models.technology",
    "app.models.person",
    "app.models.project",
    "app.models.vendor",
    "app.models.requirement",
    "app.models.availability",
    "app.models.resume_template",
    "app.models.resume_session",
    "app.models.resume_version",
    "app.models.submission",
    "app.models.ai_interaction",
    "app.models.validation_warning",
    "app.models.audit",
    "app.models.role_timeline",
)


def import_all_models() -> None:
    for module in _MODEL_MODULES:
        importlib.import_module(module)
