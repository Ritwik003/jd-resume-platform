"""Install the shipped template library into the database.

Idempotent: run it as often as you like. A template is matched by code, and
its design, description and blueprint are brought up to date — so editing a
file in ``app/resume_templates/library`` and re-running this is the whole
update path.

    python scripts/seed_template_library.py            # install / refresh
    python scripts/seed_template_library.py --verify   # also compile each one

These rows carry ``jinja_source`` from the moment they are created, which is
what makes them fillable without a model call: composing a resume in one is a
render and a compile, deterministic and about a second.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The repo, ahead of any installed copy of this package — the same bootstrap
# scripts/seed.py uses, and the reason this runs correctly from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.model_registry import import_all_models  # noqa: E402

import_all_models()

from app.config.database import SessionLocal  # noqa: E402
from app.models.resume_template import ResumeTemplate  # noqa: E402
from app.resume_templates import library  # noqa: E402


def seed_library(*, verify: bool = False) -> list[str]:
    """Create or refresh every library template. Returns what changed."""
    changes: list[str] = []

    with SessionLocal() as db:
        for entry in library.TEMPLATES:
            source = entry.source()
            # Each template's OWN section list -- a design is also a shape, and
            # some templates in this library nest projects under the employer
            # or trim to the essentials rather than all sharing one blueprint.
            blueprint = entry.blueprint().to_dict()

            if verify:
                from app.schemas.resume_content import ResumeContent
                from app.services import content_window as cw
                from app.services import template_blueprint as tb
                from app.services.rendering import latex
                from app.services.templatize import sample_content

                content: ResumeContent = sample_content()
                content.sections = []
                cw.seed(content, type("Row", (), {"blueprint": blueprint, "jinja_source": None})())
                rendered = tb.render(source, tb.build_context(content, cw.blueprint(content)))
                latex.compile_to_bytes(rendered)

            row = db.query(ResumeTemplate).filter(ResumeTemplate.code == entry.code).first()
            if row is None:
                # The name is unique in the schema, so a template already using
                # it has to be reported rather than left to surface as a wall of
                # SQL from the flush.
                clash = (
                    db.query(ResumeTemplate)
                    .filter(ResumeTemplate.name == entry.name, ResumeTemplate.code != entry.code)
                    .first()
                )
                if clash is not None:
                    raise SystemExit(
                        f"Cannot install '{entry.code}': the name {entry.name!r} is already "
                        f"used by template '{clash.code}'. Rename that one, or rename this "
                        f"entry in app/resume_templates/library/__init__.py."
                    )
                row = ResumeTemplate(code=entry.code, layout_definition={})
                db.add(row)
                changes.append(f"created {entry.code}")
            elif not row.is_active:
                changes.append(f"activated {entry.code} ({entry.name})")
            elif row.jinja_source != source or row.description != entry.description:
                changes.append(f"updated {entry.code}")

            row.name = entry.name
            row.description = entry.description
            row.source_format = "LATEX"
            row.source_file_name = entry.file
            row.jinja_source = source
            row.blueprint = blueprint
            # Every library design is switched on, new or not. These are the
            # product's BASE templates: they ship with the software, they are
            # compile-verified before release, and a fresh clone is expected to
            # offer all of them. This deliberately replaced an earlier rule that
            # only activated new rows -- that rule meant a design hidden once
            # (during a demo, say, to shorten the picker) stayed invisible
            # forever, with re-running the installer having no effect and no
            # sign of why. Hiding one is still possible from the Templates
            # screen; re-running this restores the shipped set.
            row.is_active = True
            # Built-in: nothing was uploaded, and the design is self-contained,
            # so there is no project directory to compile beside.
            row.project_dir = None
            row.main_tex_name = None
            row.derived_by_model = None

        db.commit()
    return changes


if __name__ == "__main__":
    verify = "--verify" in sys.argv
    for line in seed_library(verify=verify) or ["nothing to change"]:
        print(line)
    print(f"{len(library.TEMPLATES)} templates in the library")
