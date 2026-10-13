r"""The shipped template library — designs written against the content window.

An uploaded Overleaf design has to be *converted* before this platform can
fill it in, and that conversion depends on a model call, on the design being
compilable by the engine installed here, and on the design's own fonts being
available. Every one of those is a way for a template to arrive broken.

These templates are the other direction. Each is written by hand against the
one context ``template_blueprint.build_context`` produces, so the fit is exact
by construction rather than by inference:

* every one drives its page from the same ``sections`` loop, so removing,
  renaming, reordering or adding a section works identically in all of them;
* every one prints from the same field names, so the content window means the
  same thing whichever design is chosen;
* every one uses packaged TeX fonts rather than system fonts, and no pdfTeX-only
  primitives, so they compile with the engine that ships here;
* every one is compile-verified against a full resume, an empty one, and a
  reordered one before it is seeded.

The designs themselves are deliberately unalike — one column and two, serif and
sans, banner and sidebar and timeline — because the point of a library is
choice, not a palette swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services import template_blueprint as tb

LIBRARY_DIR = Path(__file__).resolve().parent


#: The section list every shipped template carries BY DEFAULT. A design is
#: also a *shape* — some resumes read better with projects folded into the
#: employer that owns them, some read better trimmed to the essentials — so
#: this is a default, not a rule: a template may override it with its own
#: ``sections`` below. Every field renderer already supports fewer sections
#: (``job.projects`` carries a company's own work whether or not there is a
#: standalone Projects section printing them again), so the shape genuinely
#: varies rather than the page just looking different around identical text.
CANONICAL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Professional Summary", tb.KIND_SUMMARY),
    ("Technical Skills", tb.KIND_SKILLS),
    ("Professional Experience", tb.KIND_EXPERIENCE),
    ("Key Projects", tb.KIND_PROJECTS),
    ("Education", tb.KIND_EDUCATION),
    ("Certifications", tb.KIND_CERTIFICATIONS),
)

#: Projects printed inside each employer's entry rather than as their own
#: section — "Acme Corp, 2021-2023" followed directly by the projects done
#: there. No Key Projects heading; the design's own Experience branch prints
#: ``job.projects``. Still carries Education and Certifications.
NESTED_PROJECT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Professional Summary", tb.KIND_SUMMARY),
    ("Technical Skills", tb.KIND_SKILLS),
    ("Professional Experience", tb.KIND_EXPERIENCE),
    ("Education", tb.KIND_EDUCATION),
    ("Certifications", tb.KIND_CERTIFICATIONS),
)

#: The essentials only: summary, skills, one experience section (with its
#: projects nested the same way as above). No Education or Certifications
#: heading printed by default — a candidate whose resume carries them still
#: has that content; the "unused" panel in the content window offers to add
#: the section back the moment it is wanted, so nothing is lost, only hidden
#: until asked for.
ESSENTIALS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Professional Summary", tb.KIND_SUMMARY),
    ("Relevant Skills", tb.KIND_SKILLS),
    ("Experience", tb.KIND_EXPERIENCE),
)


@dataclass(frozen=True)
class LibraryTemplate:
    code: str
    name: str
    description: str
    file: str
    #: Overrides CANONICAL_SECTIONS when given — see the three tuples above.
    sections: tuple[tuple[str, str], ...] | None = None

    @property
    def path(self) -> Path:
        return LIBRARY_DIR / self.file

    def source(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def blueprint(self) -> tb.Blueprint:
        """This template's own section list — its shape, not just its skin."""
        rows = self.sections or CANONICAL_SECTIONS
        return tb.Blueprint(
            sections=[
                tb.Section(
                    key=title.lower().replace(" ", "_"),
                    title=title,
                    kind=kind,
                    enabled=True,
                    macro="\\section",
                )
                for title, kind in rows
            ]
        )


def canonical_blueprint() -> tb.Blueprint:
    """The default blueprint — kept for callers with no specific template in
    hand (e.g. previewing a section list before one is chosen)."""
    return LibraryTemplate("", "", "", "", sections=CANONICAL_SECTIONS).blueprint()


TEMPLATES: tuple[LibraryTemplate, ...] = (
    LibraryTemplate(
        "LIB_CLASSIC",
        "Classic Column",
        "Traditional one-column serif resume: centred name, ruled small-caps headings. "
        "Projects print under the employer they were built for rather than in their "
        "own section. The safe choice for banks, consultancies and enterprise clients.",
        "classic_serif.tex.jinja",
        sections=NESTED_PROJECT_SECTIONS,
    ),
    LibraryTemplate(
        "LIB_MODERN",
        "Modern Accent",
        "Contemporary one-column sans-serif with a coloured accent rule under each "
        "heading and a left-aligned header. Reads well on screen.",
        "modern_sans.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_SIDEBAR",
        "Sidebar Rail",
        "Two columns: a tinted sidebar carrying contact, skills, education and "
        "certifications, with experience and projects filling the main column.",
        "sidebar.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_TIMELINE",
        "Career Timeline",
        "A vertical rule down the page with a marker at every role, so the career "
        "reads as a sequence at a glance.",
        "timeline.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_BANNER",
        "Colour Banner",
        "A full-width colour banner carrying the name and contact details, over a "
        "compact single-column body. Strong first impression.",
        "banner.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_MINIMAL",
        "Quiet Minimal",
        "No colour, no rules, wide margins and letter-spaced headings. Trimmed to "
        "summary, skills and experience by default — projects nest under the "
        "employer, education and certifications stay off the page until asked for.",
        "minimal.tex.jinja",
        sections=ESSENTIALS_SECTIONS,
    ),
    LibraryTemplate(
        "LIB_ATS",
        "ATS Plain Text",
        "Deliberately plain and dense: one column, no colour, no boxes, standard "
        "headings. Built to survive automated parsers and to fit the most content.",
        "ats_plain.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_BOXED",
        "Boxed Panels",
        "Each section sits in its own tinted, rounded panel — a card-like layout "
        "that separates the sections clearly.",
        "boxed.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_ACADEMIC",
        "Academic CV",
        "Palatino, large section headings and roomy leading, in the shape a "
        "curriculum vitae takes. Projects print under the employer that owns them "
        "rather than as their own section. Suits research, teaching and senior specialists.",
        "academic.tex.jinja",
        sections=NESTED_PROJECT_SECTIONS,
    ),
    LibraryTemplate(
        "LIB_EXECUTIVE",
        "Executive Two-Tone",
        "Dates in their own right-hand column, accent-coloured job titles and a "
        "restrained rule under the name. Aimed at senior and leadership profiles.",
        "executive.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_RIGHT",
        "Contact Right",
        "Name on the left, contact details stacked hard right, ruled uppercase "
        "headings. One column, plain text in reading order — parser-safe.",
        "ats_contact_right.tex.jinja",
    ),
    LibraryTemplate(
        "LIB_COMPACT",
        "Compact Dense",
        "Tight leading and small ruled headings, to fit the most experience on "
        "one page. Projects sit inside each employer's entry, not their own "
        "section. One column, no colour — the safest thing to put through an ATS.",
        "ats_compact.tex.jinja",
        sections=NESTED_PROJECT_SECTIONS,
    ),
    LibraryTemplate(
        "LIB_ACCENT",
        "Accent Headings",
        "One column with colour on the section headings only; the text itself "
        "stays black on white, so a parser reads it like any plain resume. Trimmed "
        "to summary, skills and experience by default, projects nested under each employer.",
        "ats_accent.tex.jinja",
        sections=ESSENTIALS_SECTIONS,
    ),
)
