"""Clear the work, keep the pool.

Everything this platform *produces* — requirements, sessions, versions, the
chat, the warnings, the rendered files, the submissions and the reservations
they took — is cleared. Everything it *draws from* is left alone: people,
their profiles, projects, companies, technologies, vendors, users and the
templates.

That is the line worth drawing, because the pool is the asset. It took real
work to enter and nothing about a finished demo justifies losing it, while the
sessions on top are regenerated in a minute.

    python scripts/reset_workspace.py            # show what would go
    python scripts/reset_workspace.py --yes      # actually clear it
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.model_registry import import_all_models  # noqa: E402

import_all_models()

from app.config import FILE_STORAGE_ROOT  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402

#: Deepest first — every table here is downstream of the one after it, so this
#: order needs no cascade and no deferred constraints.
WORK_TABLES = (
    "submission_status_history",
    "submissions",
    "resume_files",
    "validation_warnings",
    "ai_interactions",
    "resume_versions",
    "resume_selected_technologies",
    "resume_selected_projects",
    "resume_selected_companies",
    "resume_composition_sessions",
    "profile_reservations",
    # A vendor status is a record of THIS round of putting people forward:
    # who was engaged, who was rejected. Clearing it returns everyone to
    # AVAILABLE, which is what "start again" means.
    "profile_vendor_statuses",
    "requirement_technologies",
    "requirements",
    "audit_logs",
)

#: Named so the report can say what is being kept, not just what is going.
POOL_TABLES = (
    "persons",
    "person_profiles",
    "person_technologies",
    "person_profile_projects",
    "person_profile_technologies",
    "projects",
    "project_technologies",
    "companies",
    "employments",
    "technologies",
    "vendors",
    "profile_types",
    "app_users",
    "resume_templates",
)


def counts(db, tables) -> dict[str, int]:
    return {t: db.execute(text(f"select count(*) from {t}")).scalar() or 0 for t in tables}


def main() -> int:
    apply = "--yes" in sys.argv

    with SessionLocal() as db:
        work = counts(db, WORK_TABLES)
        pool = counts(db, POOL_TABLES)

        print("WORK — cleared:")
        for name, n in work.items():
            print(f"   {name:34} {n}")
        print("\nPOOL — kept:")
        for name, n in pool.items():
            print(f"   {name:34} {n}")

        if not apply:
            print("\nNothing changed. Re-run with --yes to clear the work.")
            return 0

        for name in WORK_TABLES:
            db.execute(text(f"delete from {name}"))
        db.commit()

        after = counts(db, WORK_TABLES)
        left = {k: v for k, v in after.items() if v}
        print("\ncleared." + ("" if not left else f" still holding rows: {left}"))

    # The rendered .tex/.pdf/.docx belong to versions that no longer exist.
    resumes = Path(FILE_STORAGE_ROOT) / "resumes"
    if resumes.is_dir():
        shutil.rmtree(resumes, ignore_errors=True)
        print(f"removed {resumes}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
