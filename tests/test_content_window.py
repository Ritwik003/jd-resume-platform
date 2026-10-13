"""The blueprint pipeline: an uploaded project becomes a fillable template.

These exercise the path that matters and does not need a model or a database:
a multi-file project is unpacked, its main file found and flattened, its
sections read, and the content window built from them behaves the way the
studio and the chat drive it. The Jinja half is proved by rendering a template
of the shape ``templatize`` is asked to produce.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.schemas.resume_content import ResumeContent, SectionSpec
from app.services import content_window as cw
from app.services import template_blueprint as tb
from app.services import template_project as tp
from app.services.templatize import sample_content
from app.utils.exceptions import ValidationError

MAIN_TEX = r"""
\documentclass[11pt]{article}
\usepackage{mycv}
\begin{document}
\name{Alex Sample}
\input{sections/experience}
\cvsection{Academic Background}
  B.E. Computer Science, University of Pune, 2018
\end{document}
"""

EXPERIENCE_TEX = r"""
\cvsection{Professional Experience}
  \begin{cvsubsection}{Acme}{2019 -- 2021}{Engineer}{Pune}
    \item Did the thing
  \end{cvsubsection}
\cvsection{Core Competencies}
  Python, SQL
"""


def _project(tmp_path):
    root = tmp_path / "proj"
    tp.unpack(
        [
            ("main.tex", MAIN_TEX.encode()),
            ("sections/experience.tex", EXPERIENCE_TEX.encode()),
            ("mycv.sty", b"\\ProvidesPackage{mycv}\n"),
        ],
        root,
    )
    return root


# ───────────────────────────────────────────────────────────── the project
def test_unpack_finds_the_main_file_and_keeps_the_style_file(tmp_path):
    root = _project(tmp_path)
    assert (root / "mycv.sty").is_file()
    assert tp.find_main_tex(root).name == "main.tex"


def test_flatten_pulls_in_inputs_but_leaves_packages_alone(tmp_path):
    root = _project(tmp_path)
    flat = tp.flatten(tp.find_main_tex(root), root)
    assert "Professional Experience" in flat  # came from the \input
    assert r"\usepackage{mycv}" in flat  # a package is not a file to inline


def test_a_zip_is_unpacked_and_its_wrapper_folder_stripped(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("overleaf-project/main.tex", MAIN_TEX)
        archive.writestr("overleaf-project/mycv.sty", "\\ProvidesPackage{mycv}\n")
        archive.writestr("overleaf-project/__MACOSX/junk", "x")
    root = tmp_path / "zipped"
    tp.unpack([("project.zip", buffer.getvalue())], root)
    assert (root / "main.tex").is_file()
    assert (root / "mycv.sty").is_file()
    assert not (root / "overleaf-project").exists()


def test_a_project_with_no_documentclass_is_refused(tmp_path):
    root = tmp_path / "fragment"
    tp.unpack([("part.tex", b"\\section{Skills}\n")], root)
    with pytest.raises(ValidationError):
        tp.find_main_tex(root)


def test_a_zip_cannot_write_outside_its_directory(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.tex", MAIN_TEX)
        archive.writestr("main.tex", MAIN_TEX)
    root = tmp_path / "safe"
    tp.unpack([("evil.zip", buffer.getvalue())], root)
    assert not (tmp_path / "escaped.tex").exists()
    assert (root / "main.tex").is_file()


# ─────────────────────────────────────────────────────────── the blueprint
def test_the_blueprint_is_the_designs_own_sections_in_its_own_words(tmp_path):
    root = _project(tmp_path)
    blueprint = tb.detect_sections(tp.flatten(tp.find_main_tex(root), root))
    titles = [s.title for s in blueprint.sections]
    assert titles == ["Professional Experience", "Core Competencies", "Academic Background"]
    kinds = {s.title: s.kind for s in blueprint.sections}
    assert kinds["Professional Experience"] == tb.KIND_EXPERIENCE
    assert kinds["Core Competencies"] == tb.KIND_SKILLS
    assert kinds["Academic Background"] == tb.KIND_EDUCATION
    # A \begin{cvsubsection} is one job, not a section of the resume.
    assert "Acme" not in titles


class _Template:
    """Just enough of a ResumeTemplate row for the window to seed from."""

    def __init__(self, blueprint):
        self.blueprint = blueprint
        self.jinja_source = None


def _seeded(tmp_path):
    root = _project(tmp_path)
    blueprint = tb.detect_sections(tp.flatten(tp.find_main_tex(root), root))
    content = sample_content()
    assert cw.seed(content, _Template(blueprint.to_dict()))
    return content


# ────────────────────────────────────────────────────── the content window
def test_the_window_is_the_templates_sections(tmp_path):
    content = _seeded(tmp_path)
    assert [s.title for s in content.sections] == [
        "Professional Experience",
        "Core Competencies",
        "Academic Background",
    ]


def test_seeding_is_idempotent_but_a_new_template_replaces_the_window(tmp_path):
    content = _seeded(tmp_path)
    other = _Template({"sections": [{"key": "profile", "title": "Profile", "kind": "summary"}]})
    assert cw.seed(content, other) is False
    assert cw.seed(content, other, force=True) is True
    assert [s.title for s in content.sections] == ["Profile"]


def test_a_section_is_found_by_heading_key_or_generic_name(tmp_path):
    content = _seeded(tmp_path)
    for needle in ("Academic Background", "academic_background", "education", "Education"):
        found = cw.find(content, needle)
        assert found is not None and found.title == "Academic Background", needle


def test_removing_a_section_hides_it_and_keeps_its_content(tmp_path):
    content = _seeded(tmp_path)
    changed, message = cw.remove(content, "education")
    assert changed and "Academic Background" in message
    assert cw.find(content, "education").enabled is False
    # The data is untouched, so putting it back costs nothing.
    assert content.education
    assert [s.kind for s in content.sections if s.enabled] == [
        tb.KIND_EXPERIENCE,
        tb.KIND_SKILLS,
    ]
    changed, _ = cw.add(content, "Academic Background")
    assert changed and cw.find(content, "education").enabled is True


def test_a_section_the_design_lacks_is_added_in_the_designs_own_style(tmp_path):
    content = _seeded(tmp_path)
    content.sections[0].macro = "\\cvsection"
    changed, _ = cw.add(content, "Publications", items=["A paper"])
    assert changed
    added = cw.find(content, "Publications")
    assert added.added is True and added.kind == tb.KIND_CUSTOM
    assert added.macro == "\\cvsection"
    assert [c.title for c in content.custom_sections] == ["Publications"]


def test_a_heading_is_matched_to_the_content_it_names(tmp_path):
    # "Awards" is how a great many designs spell the certifications section,
    # so adding one has to find the certifications rather than creating an
    # empty block beside them.
    content = _seeded(tmp_path)
    assert cw.add(content, "Awards")[0]
    assert cw.find(content, "Awards").kind == tb.KIND_CERTIFICATIONS
    row = next(r for r in cw.describe(content)["sections"] if r["title"] == "Awards")
    assert row["empty"] is False


def test_an_empty_section_is_a_legitimate_request(tmp_path):
    content = _seeded(tmp_path)
    changed, _ = cw.add(content, "Speaking Engagements")
    assert changed
    assert cw.find(content, "Speaking Engagements") is not None
    row = next(r for r in cw.describe(content)["sections"] if r["title"] == "Speaking Engagements")
    assert row["empty"] is True


def test_renaming_moves_the_custom_content_with_the_heading(tmp_path):
    content = _seeded(tmp_path)
    cw.add(content, "Publications", items=["A paper"])
    changed, _ = cw.rename(content, "Publications", "Selected Publications")
    assert changed
    assert [c.title for c in content.custom_sections] == ["Selected Publications"]
    assert cw.find(content, "Selected Publications") is not None


def test_reordering_keeps_the_sections_nobody_named(tmp_path):
    content = _seeded(tmp_path)
    changed, _ = cw.reorder(content, ["education", "Core Competencies"])
    assert changed
    assert [s.title for s in content.sections] == [
        "Academic Background",
        "Core Competencies",
        "Professional Experience",
    ]


def test_moving_past_the_edge_is_refused_rather_than_silently_ignored(tmp_path):
    content = _seeded(tmp_path)
    assert cw.move(content, "Professional Experience", "up") == (
        False,
        "'Professional Experience' is already at the top.",
    )


def test_content_with_nowhere_to_print_is_reported_not_dropped(tmp_path):
    content = _seeded(tmp_path)
    # The design has no summary or projects section, and this candidate has both.
    unused = {u["kind"] for u in cw.describe(content)["unused"]}
    assert tb.KIND_SUMMARY in unused and tb.KIND_PROJECTS in unused


# ──────────────────────────────────────────────────────────── the render
JINJA = r"""\documentclass{article}
\begin{document}
<% for section in sections %>
<% if section.kind == 'experience' %>
\section{<< section.title >>}
<% for job in experience %>\textbf{<< job.company >>} << job.dates >>
<% endfor %>
<% elif section.kind == 'education' %>
\section{<< section.title >>}
<% for row in education %><< row.qualification >>, << row.institution >>
<% endfor %>
<% elif section.kind == 'custom' %>
\section{<< section.title >>}
<% for cs in custom_sections %><% if cs.title == section.title %><% for item in cs.items %>\item << item >>
<% endfor %><% endif %><% endfor %>
<% endif %>
<% endfor %>
\end{document}
"""


def _render(content):
    return tb.render(JINJA, tb.build_context(content, cw.blueprint(content)))


def test_the_window_decides_what_the_render_prints(tmp_path):
    content = _seeded(tmp_path)
    out = _render(content)
    assert "Professional Experience" in out and "Northwind Technologies" in out
    assert "Academic Background" in out

    cw.remove(content, "education")
    out = _render(content)
    assert "Academic Background" not in out
    assert "University of Pune" not in out
    assert "Professional Experience" in out


def test_an_added_section_reaches_the_render(tmp_path):
    content = _seeded(tmp_path)
    cw.add(content, "Publications", items=["Scaling settlement systems, 2023"])
    out = _render(content)
    assert "Publications" in out and "Scaling settlement systems" in out


def test_reordering_the_window_reorders_the_page(tmp_path):
    content = _seeded(tmp_path)
    cw.reorder(content, ["Academic Background"])
    out = _render(content)
    assert out.index("Academic Background") < out.index("Professional Experience")


def test_values_are_escaped_on_the_way_into_latex(tmp_path):
    content = _seeded(tmp_path)
    content.experience[0].company = "Smith & Sons #1"
    assert r"Smith \& Sons \#1" in _render(content)


def test_the_same_content_renders_identically_every_time(tmp_path):
    content = _seeded(tmp_path)
    assert _render(content) == _render(content.clone())


def test_the_window_survives_a_round_trip_through_the_stored_version(tmp_path):
    content = _seeded(tmp_path)
    cw.add(content, "Publications", items=["A paper"])
    restored = ResumeContent.model_validate(content.model_dump(mode="json"))
    assert [s.title for s in restored.sections] == [s.title for s in content.sections]
    assert isinstance(restored.sections[0], SectionSpec)
    assert _render(restored) == _render(content)


# ─────────────────────────────────────────── the composer's fast path
class _Fillable:
    """A template row that has been through the upload pipeline."""

    def __init__(self, jinja):
        self.jinja_source = jinja
        self.blueprint = {
            "sections": [
                {"key": "profile", "title": "Profile", "kind": "summary", "macro": r"\section"},
                {"key": "history", "title": "Employment History", "kind": "experience"},
            ]
        }


FILLABLE = r"""\documentclass{article}
\usepackage{xcolor}
\begin{document}
<% for section in sections %>\section{<< section.title >>}
<% if section.kind == 'summary' %><< summary >>
<% endif %><% endfor %>\end{document}
"""


def test_render_blueprint_fills_the_design_and_keeps_the_preamble():
    from app.services import latex_composer

    template = _Fillable(FILLABLE)
    content = sample_content()
    out = latex_composer.render_blueprint(template, content)

    assert r"\usepackage{xcolor}" in out  # the design survives untouched
    assert r"\section{Profile}" in out and r"\section{Employment History}" in out
    assert "Backend engineer with 7+ years" in out
    assert out.rstrip().endswith(r"\end{document}")
    # Deterministic: the same resume in the same design, twice.
    assert out == latex_composer.render_blueprint(template, sample_content())
