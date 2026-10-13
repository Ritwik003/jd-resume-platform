"""Every shipped template still fills the content window, in every shape.

The library's whole claim is that its designs fit the data by construction.
That claim is only worth something if it is checked, so each template is
rendered against four resumes — one of everything, one with sections removed,
one with sections added and reordered, and one that is nearly empty — and
compiled when an engine is present.

The render half runs everywhere and is the part that catches a template
reaching for a field that does not exist. The compile half is skipped where
there is no LaTeX engine rather than failing, because a missing engine is a
fact about the machine, not about the template.
"""

from __future__ import annotations

import pytest

from app.resume_templates import library
from app.schemas.resume_content import ContactBlock, ResumeContent
from app.services import content_window as cw
from app.services import template_blueprint as tb
from app.services.rendering import latex
from app.services.templatize import sample_content


class _Row:
    """A stand-in for the template row, carrying only what seeding sets."""

    def __init__(self) -> None:
        self.blueprint = library.canonical_blueprint().to_dict()
        self.jinja_source = None


def _seeded(content: ResumeContent) -> ResumeContent:
    cw.seed(content, _Row())
    return content


def _full() -> ResumeContent:
    return _seeded(sample_content())


def _trimmed() -> ResumeContent:
    content = _full()
    cw.remove(content, "education")
    cw.remove(content, "certifications")
    return content


def _extended() -> ResumeContent:
    content = _full()
    cw.add(content, "Publications", items=["Scaling settlement systems, 2023"])
    cw.add(content, "Security Clearance", body="Cleared to SC level, valid to 2029.")
    cw.reorder(content, ["Key Projects", "Professional Experience"])
    return content


def _bare() -> ResumeContent:
    return _seeded(
        ResumeContent(contact=ContactBlock(full_name="Jane Q. Candidate", email="jane@example.com"))
    )


SHAPES = {"full": _full, "trimmed": _trimmed, "extended": _extended, "bare": _bare}

CASES = [(t, shape) for t in library.TEMPLATES for shape in SHAPES]
IDS = [f"{t.code}-{shape}" for t, shape in CASES]


def _render(template: library.LibraryTemplate, content: ResumeContent) -> str:
    return tb.render(template.source(), tb.build_context(content, cw.blueprint(content)))


@pytest.mark.parametrize(("template", "shape"), CASES, ids=IDS)
def test_every_template_renders_every_shape(template, shape) -> None:
    content = SHAPES[shape]()
    tex = _render(template, content)

    assert "\\begin{document}" in tex and "\\end{document}" in tex
    assert content.contact.full_name in tex
    # Jinja delimiters must all have been consumed; a stray one means a block
    # was left open and the PDF would carry template syntax on the page.
    assert "<<" not in tex and "<%" not in tex

    if shape != "bare":
        # A section that is switched on and has content has to print its
        # heading, whatever the design's markup around it looks like.
        for spec in content.sections:
            if spec.enabled and spec.kind in ("experience", "skills"):
                assert spec.title in tex, f"{spec.title} missing from {template.code}"


def test_a_removed_section_leaves_nothing_behind() -> None:
    """Not just the heading: the content under it must go too."""
    for template in library.TEMPLATES:
        tex = _render(template, _trimmed())
        assert "Education" not in tex, template.code
        assert "University of Pune" not in tex, template.code
        assert "AWS Certified" not in tex, template.code


def test_an_added_section_reaches_every_design() -> None:
    for template in library.TEMPLATES:
        tex = _render(template, _extended())
        assert "Publications" in tex, template.code
        assert "Scaling settlement systems" in tex, template.code
        assert "Security Clearance" in tex, template.code


def test_reordering_reorders_every_design() -> None:
    for template in library.TEMPLATES:
        tex = _render(template, _extended())
        assert tex.index("Key Projects") < tex.index("Professional Experience"), template.code


def test_the_library_is_the_same_window_everywhere() -> None:
    """One content window across the library — the point of shipping a set."""
    expected = [title for title, _ in library.CANONICAL_SECTIONS]
    for template in library.TEMPLATES:
        content = _full()
        assert [s.title for s in content.sections] == expected, template.code


def test_rendering_is_deterministic() -> None:
    for template in library.TEMPLATES:
        content = _full()
        assert _render(template, content) == _render(template, content.clone())


@pytest.mark.skipif(not latex.available(), reason="no LaTeX engine on this machine")
@pytest.mark.parametrize(("template", "shape"), CASES, ids=IDS)
def test_every_template_compiles_every_shape(template, shape) -> None:
    pdf = latex.compile_to_bytes(_render(template, SHAPES[shape]()))
    assert pdf[:5] == b"%PDF-"


# ── shipped as base templates ────────────────────────────────────────────────
# The conftest database is created and seeded from scratch for the session, so
# asserting against it is asserting what a fresh clone gets.


def test_a_fresh_clone_offers_every_library_design() -> None:
    """All of them ship switched on — none of the set is missing or hidden."""
    from app.config.database import SessionLocal
    from app.models.resume_template import ResumeTemplate

    with SessionLocal() as db:
        rows = {
            r.code: r
            for r in db.query(ResumeTemplate)
            .filter(ResumeTemplate.code.in_([t.code for t in library.TEMPLATES]))
            .all()
        }

    missing = [t.code for t in library.TEMPLATES if t.code not in rows]
    assert not missing, f"seeding a fresh database did not install: {missing}"

    hidden = [c for c, r in rows.items() if not r.is_active]
    assert not hidden, f"installed but not offered in the picker: {hidden}"

    # Fillable without a model call — the property that makes composing in one
    # a render and a compile rather than a conversion.
    assert all(r.jinja_source for r in rows.values())


def test_reinstalling_restores_a_design_somebody_hid() -> None:
    """Hiding one is an operator's call; re-running the installer undoes it.

    Five designs spent a week invisible because the installer only switched on
    rows it had just created, so re-running it could never bring a hidden one
    back and nothing said why.
    """
    from app.config.database import SessionLocal
    from app.models.resume_template import ResumeTemplate
    from scripts.seed_template_library import seed_library

    code = library.TEMPLATES[0].code
    with SessionLocal() as db:
        db.query(ResumeTemplate).filter(ResumeTemplate.code == code).update(
            {"is_active": False}
        )
        db.commit()

    seed_library()

    with SessionLocal() as db:
        row = db.query(ResumeTemplate).filter(ResumeTemplate.code == code).first()
        assert row.is_active, f"{code} stayed hidden after re-running the installer"
