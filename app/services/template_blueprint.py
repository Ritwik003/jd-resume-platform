"""A template's own sections, and a deterministic way to fill them.

The old pipeline asked a model to write the whole ``.tex`` for every resume,
handing it the template as an example. That is why the same content landed
differently in the same design on two consecutive runs: the model was
re-deriving the design each time, and a design is not something to
re-derive — it is something to fill in.

This module replaces that with two things done ONCE, when a template is
uploaded:

1. **The blueprint** — which sections this design actually has, in its own
   order and its own wording ("Professional Experience", not "experience").
   The content window is built from this, so editing a resume edits the
   design it will be printed into. A template with no Education section
   never offers one; a template that calls its summary "Profile" says
   Profile.

2. **The Jinja source** — the same ``.tex`` with its worked example replaced
   by loops over the resume's data. Jinja's delimiters are re-bound to
   ``<< >>`` and ``<% %>`` because LaTeX has a prior claim on ``{}``.

After that, every render is ``jinja.render(context)`` followed by a compile.
Deterministic, a second rather than a minute, and identical every time.

Sections the operator adds in the studio that the design has no block for
are appended through the template's OWN section macro, captured from the
blueprint pass — so a new section looks like it belongs rather than like it
was bolted on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from jinja2 import Environment, StrictUndefined
from jinja2 import TemplateError as JinjaTemplateError

from app.schemas.resume_content import ResumeContent

logger = logging.getLogger(__name__)

#: Jinja re-bound off LaTeX's braces. ``<< value >>`` and ``<% for %>`` cannot
#: occur in real LaTeX, where ``{`` is structural and ``<`` is a maths relation.
JINJA_ENV_OPTIONS: dict[str, Any] = {
    "block_start_string": "<%",
    "block_end_string": "%>",
    "variable_start_string": "<<",
    "variable_end_string": ">>",
    "comment_start_string": "<#",
    "comment_end_string": "#>",
    "trim_blocks": True,
    "lstrip_blocks": True,
    "autoescape": False,
}

#: The kinds of content this platform composes. A section on an uploaded
#: design is matched to one of these when its title is recognisable, and left
#: as CUSTOM when it is not — an unrecognised section is still a section, and
#: dropping it would silently redesign somebody's template.
KIND_SUMMARY = "summary"
KIND_SKILLS = "skills"
KIND_EXPERIENCE = "experience"
KIND_PROJECTS = "projects"
KIND_EDUCATION = "education"
KIND_CERTIFICATIONS = "certifications"
KIND_CUSTOM = "custom"

KNOWN_KINDS = (
    KIND_SUMMARY,
    KIND_SKILLS,
    KIND_EXPERIENCE,
    KIND_PROJECTS,
    KIND_EDUCATION,
    KIND_CERTIFICATIONS,
)

#: Words a section heading uses for each kind, longest-first inside each group
#: so "professional experience" is not answered by "profile".
_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_SUMMARY, ("professional summary", "career summary", "summary", "profile",
                    "objective", "about me", "about", "overview", "synopsis")),
    (KIND_SKILLS, ("technical skills", "core competencies", "competencies", "skills",
                   "technologies", "tech stack", "expertise", "toolbox")),
    (KIND_EXPERIENCE, ("professional experience", "work experience", "employment history",
                       "experience", "employment", "work history", "career history")),
    (KIND_PROJECTS, ("selected projects", "key projects", "personal projects", "projects",
                     "portfolio")),
    (KIND_EDUCATION, ("education", "academic background", "academics", "qualifications")),
    (KIND_CERTIFICATIONS, ("certifications", "certificates", "licenses", "licences",
                           "accreditations", "awards")),
)


def classify(title: str) -> str:
    """Which kind of content a heading is asking for.

    Longest phrase wins, so "Work Experience" is experience rather than being
    caught by a shorter word inside it.
    """
    text = re.sub(r"[^a-z ]+", " ", (title or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return KIND_CUSTOM
    best: tuple[int, str] = (0, KIND_CUSTOM)
    for kind, words in _KIND_WORDS:
        for word in words:
            if word in text and len(word) > best[0]:
                best = (len(word), kind)
    return best[1]


@dataclass(slots=True)
class Section:
    """One block of a design, as the content window will show it."""

    key: str
    title: str
    kind: str
    enabled: bool = True
    #: How this section is opened in the template's own LaTeX, captured so a
    #: section the operator ADDS can be printed in the same style rather than
    #: in some default the design never uses.
    macro: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "enabled": self.enabled,
            "macro": self.macro,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Section:
        return Section(
            key=str(raw.get("key") or ""),
            title=str(raw.get("title") or ""),
            kind=str(raw.get("kind") or KIND_CUSTOM),
            enabled=bool(raw.get("enabled", True)),
            macro=raw.get("macro"),
        )


@dataclass(slots=True)
class Blueprint:
    """A design's sections, in its order, plus how it opens one."""

    sections: list[Section] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"sections": [s.to_dict() for s in self.sections]}

    @staticmethod
    def from_dict(raw: dict[str, Any] | None) -> Blueprint:
        if not isinstance(raw, dict):
            return Blueprint()
        rows = raw.get("sections")
        if not isinstance(rows, list):
            return Blueprint()
        return Blueprint(sections=[Section.from_dict(r) for r in rows if isinstance(r, dict)])


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


# ─────────────────────────────────────────────── reading a design's sections
#
# Three shapes cover essentially every resume template on Overleaf:
#
#   \section{Education}            the article class and most derivatives
#   \begin{cvsection}{Education}   an environment, e.g. the cv_template class
#   \cvsection{Education}          a bare custom macro
#
# Anything matching is a candidate; the title then has to look like a heading
# rather than a stray argument, which ``_is_heading`` decides.

#: The opener of a section, up to but not including its title argument. Real
#: templates spell it three ways and the case varies, so the environment and
#: the command forms are matched together and the title is read separately —
#: with a brace counter rather than a regex, because a heading like
#: ``\section{\color{navy}EDUCATION}`` nests braces and a regex cannot.
_SECTION_OPENER = re.compile(
    r"\\begin\s*\{(?P<env>[A-Za-z@]*[Ss]ection[A-Za-z@]*)\}\s*(?=\{)"
    r"|\\(?P<cmd>section\*?|[A-Za-z@]*[Ss]ection[A-Za-z@]*|rSection|cvsect|cvsec)\s*(?=\{)"
)
#: A subsection is an ENTRY inside a section — one job, one project — not a
#: section of the resume. Both spellings show up: \subsection{...} and a
#: custom \begin{cvsubsection}{...}, and both would otherwise match the
#: pattern above because "subsection" contains "section".
_SUBSECTION = re.compile(r"sub\s*section", re.IGNORECASE)
_COMMENT = re.compile(r"(?<!\\)%.*?$", re.MULTILINE)

#: Macros whose braced argument is a SETTING rather than visible text, so the
#: argument has to go with the macro. ``\color{navy}Education`` prints
#: "Education"; keeping "navy" would name the section after a colour.
_SETTING_MACRO = re.compile(
    r"\\(?:color|textcolor|colorbox|definecolor|hspace\*?|vspace\*?|rule|label|phantom|"
    r"hyperlink|hypertarget|raisebox|makebox|parbox)\s*\{[^{}]*\}"
)
#: Everything else — \textbf, \MakeUppercase, \Large — wraps or precedes the
#: text, so the macro goes and the text stays.
_ANY_MACRO = re.compile(r"\\[A-Za-z@]+\*?\s*")


def _strip_comments(source: str) -> str:
    return _COMMENT.sub("", source or "")


def _braced(text: str, open_at: int) -> tuple[str, int] | None:
    """The contents of the brace group starting at ``open_at``, and its end.

    Counts depth, so a heading carrying its own markup comes out whole where a
    regex would stop at the first inner ``}``. ``None`` when the group never
    closes, which means the file is truncated rather than that we found a
    section.
    """
    if open_at >= len(text) or text[open_at] != "{":
        return None
    depth = 0
    for index in range(open_at, min(len(text), open_at + 400)):
        char = text[index]
        if char == "{" and text[index - 1: index] != "\\":
            depth += 1
        elif char == "}" and text[index - 1: index] != "\\":
            depth -= 1
            if depth == 0:
                return text[open_at + 1: index], index + 1
    return None


def _clean_title(raw: str) -> str:
    r"""The words a heading actually prints, with its styling taken off.

    Designs write their headings dressed: ``\color{navy}EDUCATION``,
    ``\textbf{Work Experience}``, ``\MakeUppercase{Skills}``. The dressing
    belongs to the design and stays in the template; what the content window
    shows, and what the operator renames, is the text.
    """
    text = raw or ""
    for _ in range(3):
        stripped = _SETTING_MACRO.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = _ANY_MACRO.sub(" ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", text).strip(" ~*")


def _takes_a_row_of_arguments(text: str, after: int) -> bool:
    r"""Does this macro take several more arguments — making it a ROW, not a heading?

    A section heading is a title and nothing else: ``\section{Experience}``.
    A macro that goes on to take two further arguments is printing a line of
    content, whatever it happens to be called. The case that forced this is
    the popular "Rezume" template, whose skill rows read

        \resumeSectionType{Languages}{:}{JavaScript, PHP, Java}

    — and whose name contains "Section", so every skills row was being offered
    to the operator as a section of the resume. One extra argument is left
    alone, because a design may legitimately give a heading a subtitle or an
    icon; two is a row.
    """
    position = after
    for _ in range(2):
        while position < len(text) and text[position] in " \t":
            position += 1
        following = _braced(text, position)
        if following is None:
            return False
        position = following[1]
    return True


def _is_heading(title: str) -> bool:
    """Does this argument read as a section heading rather than as content?

    A heading is short, is words rather than markup, and does not carry the
    punctuation a sentence would.
    """
    text = (title or "").strip()
    if not text or len(text) > 60:
        return False
    if "\\" in text or "$" in text:
        return False
    if any(ch in text for ch in ".;:•"):
        return False
    return bool(re.search(r"[A-Za-z]", text))


def detect_sections(source: str) -> Blueprint:
    """The sections a design declares, in the order it declares them.

    Deliberately structural, not model-driven: a section heading is a literal
    macro call in the file, and reading it is both exact and free. What a
    heading MEANS is a separate question, answered by ``classify``.
    """
    body = _strip_comments(source)
    begin = re.search(r"\\begin\s*\{document\}", body)
    if begin is not None:
        body = body[begin.end():]

    found: list[Section] = []
    seen: set[str] = set()

    for match in _SECTION_OPENER.finditer(body):
        opener = match.group("env") or match.group("cmd") or ""
        if _SUBSECTION.search(opener):
            continue
        argument = _braced(body, match.end())
        if argument is None:
            continue
        if _takes_a_row_of_arguments(body, argument[1]):
            continue
        title = _clean_title(argument[0])
        if not _is_heading(title):
            continue
        key = _slug(title)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(
            Section(
                key=key,
                title=title,
                kind=classify(title),
                enabled=True,
                # How this design opens a section, kept so a section the
                # operator ADDS can be printed the same way.
                macro=f"\\begin{{{opener}}}" if match.group("env") else f"\\{opener}",
            )
        )

    return Blueprint(sections=found)


# ────────────────────────────────────────────── the data a template renders
#
# One shape, whatever the design. A template written for this platform gets
# the same context every time, so a second template is a second .tex rather
# than a second data model.


def build_context(content: ResumeContent, blueprint: Blueprint | None = None) -> dict[str, Any]:
    """The Jinja context for one resume.

    Everything a design could want, already shaped for printing: contact
    fields flattened, projects grouped under the employer they belong to, and
    ``sections`` carrying the order and the headings so the template can loop
    the page itself rather than hard-coding an order the operator has since
    changed.
    """
    contact = content.contact
    by_company: dict[str, list[dict[str, Any]]] = {}
    for project in content.projects:
        by_company.setdefault(project.source_company_id or "", []).append(_project_dict(project))

    experience = []
    for block in content.experience:
        experience.append(
            {
                "company": block.company or "",
                "designation": block.designation or "",
                "start_label": block.start_label or "",
                "end_label": block.end_label or "",
                "dates": " -- ".join(x for x in (block.start_label, block.end_label) if x),
                "location": block.location or "",
                "description": block.description or "",
                "highlights": list(block.highlights or []),
                "projects": by_company.get(block.source_company_id or "", []),
            }
        )

    # Projects grouped under the employer they were built at, in the employer
    # order the experience section already prints. This is what a Key Projects
    # section is filled from: a flat list repeating "| Acme" on every row says
    # the same thing five times and still leaves the reader assembling the
    # grouping in their head. Projects belonging to no employer on this resume
    # are collected last under an empty company, so nothing is silently
    # dropped from the page.
    project_groups: list[dict[str, Any]] = []
    for block in content.experience:
        owned = by_company.get(block.source_company_id or "", [])
        if owned:
            project_groups.append(
                {
                    "company": block.company or "",
                    "designation": block.designation or "",
                    "dates": " -- ".join(x for x in (block.start_label, block.end_label) if x),
                    "projects": owned,
                }
            )
    known_ids = {block.source_company_id or "" for block in content.experience}
    orphans = [
        _project_dict(p) for p in content.projects if (p.source_company_id or "") not in known_ids
    ]
    if orphans:
        project_groups.append({"company": "", "designation": "", "dates": "", "projects": orphans})

    sections: list[dict[str, Any]] = []
    if blueprint is not None:
        sections = [s.to_dict() for s in blueprint.sections if s.enabled]

    # Does this resume print a Key Projects section of its own?
    #
    # This is what decides the SHAPE, and it is read off the section list
    # rather than baked into each design. With a projects section, the
    # employer entry carries its remit and the projects print grouped under
    # their employer below. Without one, the projects nest inside the employer
    # that owns them. Every template writes both branches the same way and
    # asks this flag, so the two shapes cannot drift apart across thirteen
    # designs, and — the bug this replaces — turning a section off can no
    # longer make work disappear from the page: hide Professional Experience
    # and the projects still print in their own section; hide Key Projects and
    # they fold back under the employers.
    projects_section_visible = (
        any(s.get("kind") == KIND_PROJECTS for s in sections) if sections else True
    )

    return {
        "contact": {
            "full_name": contact.full_name or "",
            "headline": contact.headline or "",
            "email": contact.email or "",
            "phone": contact.phone or "",
            "location": contact.location or "",
            "links": [str(x) for x in (getattr(contact, "links", None) or [])],
        },
        "summary": content.summary or "",
        "skills": [
            {"label": g.label or "", "items": list(g.items or []), "joined": ", ".join(g.items or [])}
            for g in content.skill_groups
            if g.items
        ],
        "experience": experience,
        "projects": [_project_dict(p) for p in content.projects],
        "project_groups": project_groups,
        "projects_section_visible": projects_section_visible,
        "education": [
            {
                "qualification": e.qualification or "",
                "institution": e.institution or "",
                "year": e.year or "",
            }
            for e in content.education
        ],
        "certifications": list(content.certifications or []),
        "custom_sections": [
            {"title": s.title or "", "body": s.body or "", "items": list(s.items or [])}
            for s in content.custom_sections
            if (s.title or "").strip()
        ],
        "sections": sections,
    }


def _project_dict(project: Any) -> dict[str, Any]:
    return {
        "name": project.name or "",
        "company": project.company or "",
        "description": project.description or "",
        "responsibilities": list(project.responsibilities or []),
        "achievements": list(project.achievements or []),
        "bullets": list(project.responsibilities or []) + list(project.achievements or []),
        "technologies": list(project.technologies or []),
        "tech_joined": ", ".join(project.technologies or []),
    }


# ──────────────────────────────────────────────────────── escaping and render
#
# Everything in the context is prose the operator typed or a model wrote, and
# LaTeX reads several ordinary characters as instructions. Escaping happens
# on the way INTO the render rather than being left to each template author,
# because a stray "&" in a company name should not be able to break a
# compile.

_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex(value: Any) -> Any:
    """Make one value safe to drop into LaTeX, recursing through containers."""
    if isinstance(value, str):
        return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in value)
    if isinstance(value, list):
        return [escape_latex(v) for v in value]
    if isinstance(value, dict):
        return {k: escape_latex(v) for k, v in value.items()}
    return value


class Blank(str):
    """Nothing, in every shape a template might ask for it.

    A design asking for ``job.location`` when this resume has no location for
    that job should print nothing, not fail. Strictness is right for
    *undefined names* — a template asking for a variable that does not exist
    is a template bug — but a missing FIELD is ordinary data, and every
    resume has some. So an unknown attribute yields this: an empty string
    that is falsey, iterates as empty, and answers any further attribute with
    another one, so ``job.location.city`` is empty rather than an exception.
    """

    __slots__ = ()

    def __new__(cls) -> Blank:
        return super().__new__(cls, "")

    def __getattr__(self, name: str) -> Blank:
        if name.startswith("__"):
            raise AttributeError(name)
        return Blank()

    def __getitem__(self, key: Any) -> Blank:
        return Blank()

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def __call__(self, *args: Any, **kwargs: Any) -> Blank:
        return Blank()


#: The names a template plausibly reaches for when a list holds plain strings —
#: ``<% for cert in certifications %><< cert.name >>``. The string answers to
#: all of them with itself, so a template written that way prints the
#: certification rather than crashing on a str with no attributes.
_TEXT_ALIASES = frozenset(
    {"name", "label", "title", "text", "value", "item", "description", "body"}
)


class Text(str):
    """A string that also answers to the field names a template might use."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _TEXT_ALIASES:
            return Text(str(self))
        return Blank()


class Box:
    """A mapping whose keys are reachable as attributes — data first.

    Deliberately NOT a dict subclass. Jinja resolves ``group.items`` with
    ``getattr`` before falling back to the key, and on a real dict that finds
    the built-in ``.items()`` method rather than the list of skills — so a
    template written the natural way silently iterates a method. Template
    authors, human and model alike, write ``group.items``; here the data wins
    that lookup because there is no method to lose to.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return object.__getattribute__(self, "_data").get(name, Blank())

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_data").get(key, Blank())

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")

    def __iter__(self):
        return iter(object.__getattribute__(self, "_data"))

    def keys(self):
        return object.__getattribute__(self, "_data").keys()

    def get(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_data").get(key, default)

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_data"))

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Box({object.__getattribute__(self, '_data')!r})"


def boxed(value: Any) -> Any:
    """Recursively make every mapping attribute-accessible and every string forgiving."""
    if isinstance(value, dict):
        return Box({k: boxed(v) for k, v in value.items()})
    if isinstance(value, list):
        return [boxed(v) for v in value]
    if isinstance(value, str):
        return Text(value)
    return value


def render(jinja_source: str, context: dict[str, Any]) -> str:
    """Fill a templatized design with one resume's data.

    Undefined names raise rather than rendering empty: a template asking for
    something the context does not have is a template bug, and finding it on
    the first render is much cheaper than finding it in a PDF somebody has
    already sent.
    """
    env = Environment(undefined=StrictUndefined, **JINJA_ENV_OPTIONS)
    env.filters["tex"] = escape_latex
    template = env.from_string(jinja_source)
    return template.render(**boxed(escape_latex(context)))


class TemplatizeError(RuntimeError):
    """The uploaded design could not be turned into a fillable template."""


def validate_jinja(jinja_source: str) -> None:
    """Does this compile as a Jinja template at all? Raises if not."""
    env = Environment(undefined=StrictUndefined, **JINJA_ENV_OPTIONS)
    env.filters["tex"] = escape_latex
    try:
        env.from_string(jinja_source)
    except JinjaTemplateError as exc:  # pragma: no cover - message is the value
        raise TemplatizeError(f"The templatized design is not valid Jinja: {exc}") from exc
