r"""Fill an uploaded LaTeX template with a candidate's history, angled at a JD.

This is the "twist" in the workflow: the first version a user sees is not a
transcription of the stored profile, it is that profile moulded toward the
requirement — the title set to the JD's role, the strongest matching work first,
descriptions rewritten in the JD's vocabulary where the underlying work really
matches, and skills reordered around the mandatory list.

The base truth is never edited. The moulding lives only in the generated
document, which is exactly the separation section 4 asks for: master data stays
reusable, every resume is a separate composition.

How a template is filled
------------------------
The uploaded .tex is split at ``\begin{document}`` and the two halves are
treated completely differently:

* **The preamble is copied byte for byte.** It is never sent to the model to be
  reproduced. Asking a model to re-emit five kilobytes of ``\usepackage``,
  ``\titleformat`` and ``\newcommand`` verbatim is a losing game — it dropped
  the backslash from every ``\renewcommand`` in one real template, and the
  compile died on line 34 with "Missing \begin{document}". Copying guarantees
  the design survives, for this template and for every template uploaded later.
* **Only the body is generated**, using the macros the preamble defines, with
  the template's own body supplied as the worked example of how to use them.

Contact placeholders that live in the preamble (``\def\name{Your Name}``) are
substituted deterministically in Python, and blanked when the candidate has no
value — a verbatim preamble would otherwise print the template author's real
LinkedIn handle on someone else's resume.
"""

from __future__ import annotations

import json
import logging
import re
from typing import NamedTuple

from sqlalchemy.orm import Session

from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS
from app.models.requirement import Requirement
from app.models.resume_session import ResumeCompositionSession
from app.models.resume_template import ResumeTemplate
from app.prompts.templates import (
    LATEX_BODY_REPAIR,
    LATEX_BODY_SYSTEM,
    LATEX_BODY_USER,
    LATEX_RESUME_SYSTEM,
    LATEX_RESUME_USER,
)
from app.repositories import requirement_repository, template_repository
from app.schemas.resume_content import ResumeContent
from app.services import content_window, presentation, storage
from app.services import template_blueprint as tb
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry
from app.services.rendering.latex import escape  # noqa: F401 — re-exported for callers
from app.utils.exceptions import ValidationError

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:latex|tex)?\s*|\s*```\s*$", re.I)
_DOC_START = re.compile(r"(\\documentclass|\\input|\\RequirePackage)")


def read_template_source(template: ResumeTemplate) -> str:
    """The LaTeX this template renders from.

    A template uploaded as PDF or Word was converted to LaTeX once at upload and
    that conversion is the thing we render — the original file stays on disk so
    the operator can still download what they gave us, but it is a design
    reference, not a fillable source.
    """
    derived = (getattr(template, "derived_latex", None) or "").strip()
    if derived:
        return derived

    if not template.source_storage_path:
        raise ValidationError(
            f"Template '{template.name}' has no LaTeX source. Upload a .tex, .pdf or .docx for it."
        )
    path = storage.absolute_path(template.source_storage_path)
    if not path.exists():
        raise ValidationError(f"The source file for '{template.name}' is missing from storage.")
    if path.suffix.lower() != ".tex":
        raise ValidationError(
            f"Template '{template.name}' was uploaded as {path.suffix or 'an unknown format'} "
            f"but has no LaTeX conversion stored. Re-upload it so it can be converted."
        )
    return path.read_text(encoding="utf-8", errors="replace")


_BEGIN_DOC = re.compile(r"\\begin\s*\{document\}")
_END_DOC = re.compile(r"\\end\s*\{document\}")


class TemplateParts(NamedTuple):
    r"""An uploaded template cut at ``\begin{document}``."""

    #: Everything up to and including ``\begin{document}``. Copied verbatim.
    preamble: str
    #: What sits between the document delimiters — the part that is regenerated.
    body: str


def split_document(source: str) -> TemplateParts | None:
    r"""Cut a template into preamble and body, or ``None`` if it is a fragment.

    A template with no ``\begin{document}`` is a body-only snippet, and there is
    no preamble to protect; those fall back to whole-document composition.
    """
    begin = _BEGIN_DOC.search(source)
    if begin is None:
        return None
    end = _END_DOC.search(source, begin.end())
    body = source[begin.end() : end.start()] if end else source[begin.end() :]
    return TemplateParts(preamble=source[: begin.end()], body=body)


#: Preamble placeholders and where their value comes from. A verbatim preamble
#: keeps whatever the template author wrote, so a placeholder we recognise but
#: cannot fill is BLANKED rather than left — printing a stranger's phone number
#: or LinkedIn handle on a candidate's resume is the worst failure here.
_PLACEHOLDERS: dict[str, str] = {
    "name": "full_name",
    "fullname": "full_name",
    "myname": "full_name",
    "candidatename": "full_name",
    "phone": "phone",
    "mobile": "phone",
    "tel": "phone",
    "telephone": "phone",
    "email": "email",
    "mail": "email",
    "emailaddress": "email",
    "city": "location",
    "location": "location",
    "address": "location",
    "role": "headline",
    "jobtitle": "headline",
    "position": "headline",
    "linkedin": "",
    "github": "",
    "portfoliourl": "",
    "portfolioname": "",
    "website": "",
    "twitter": "",
}

_DEF_LINE = re.compile(
    r"(?P<head>\\(?:def|newcommand\*?|renewcommand\*?)\s*\{?\\(?P<key>[A-Za-z]+)\}?\s*)"
    r"\{(?P<value>[^{}]*)\}"
)


def fill_preamble_placeholders(preamble: str, content: ResumeContent) -> str:
    r"""Replace ``\def\name{Your Name}``-style contact fields in the preamble.

    Deterministic, not model-driven: these are single values with one correct
    answer, and the preamble is the one part of the document a model is not
    allowed to touch.
    """
    contact = content.contact
    values = {
        "full_name": (contact.full_name or "").strip(),
        "phone": (contact.phone or "").strip(),
        "email": (contact.email or "").strip(),
        "location": (contact.location or "").strip(),
        "headline": (contact.headline or "").strip(),
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group("key").lower()
        if key not in _PLACEHOLDERS:
            return match.group(0)
        field = _PLACEHOLDERS[key]
        value = values.get(field, "") if field else ""
        return f"{match.group('head')}{{{escape(value)}}}"

    return _DEF_LINE.sub(replace, preamble)


#: A body that is nothing but \input lines has no structure to mirror, and a
#: preamble that loads a missing .sty has no macros to honour. Either way the
#: uploaded file is one part of a multi-file project and cannot be reproduced.
_ONLY_INPUTS = re.compile(r"\\(?:input|include)\{[^}]*\}|%[^\n]*|\s+")

_DOCUMENTCLASS = re.compile(r"\\documentclass(\[[^\]]*\])?\{([^}]*)\}")

#: Document classes that ship with any TeX distribution. Anything else in a
#: ``\documentclass`` is a file that was meant to sit next to the template.
_STANDARD_CLASSES = {
    "article", "report", "book", "letter", "memoir", "proc", "slides",
    "extarticle", "extreport", "extbook", "extletter",
    "scrartcl", "scrreprt", "scrbook", "scrlttr2",
    "moderncv", "europecv", "amsart", "amsbook", "beamer", "standalone",
}


#: An unescaped ``%`` comments out the rest of the line. Templates carry plenty
#: of commented-out alternatives — one ships ``% \input{glyphtounicode}`` for
#: pdflatex users — and counting those as missing files would send a perfectly
#: self-contained template down the reconstruct-everything path.
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")


def _strip_comments(source: str) -> str:
    return _COMMENT.sub("", source)


def missing_dependencies(source: str) -> list[str]:
    r"""Files the template pulls in that were never uploaded.

    Overleaf resume templates are usually a project, not a file: the main .tex
    ``\input``s a header and per-section files and loads a style file sitting
    next to it. Only the main file reaches us, so those references name things
    that do not exist. Reporting them is what lets the operator fix the real
    problem instead of wondering why the output ignores their design.

    A ``\documentclass`` naming something outside the standard set counts too.
    Resume templates very often ship their own ``resume.cls`` beside the .tex,
    and without this the file looks perfectly self-contained right up until the
    compile dies on "File `resume.cls' not found" — which is a demo failing in
    front of somebody, not a log line.
    """
    source = _strip_comments(source)
    found = [f"{m.group(1)}.tex" for m in re.finditer(r"\\(?:input|include)\{([^}]*)\}", source)]
    found += [f"{m.group(2)}.sty" for m in _LOCAL_STYLE_ANY.finditer(source)]
    cls = _DOCUMENTCLASS.search(source)
    if cls is not None and cls.group(2).strip().lower() not in _STANDARD_CLASSES:
        found.append(f"{cls.group(2).strip()}.cls")
    seen: set[str] = set()
    unique: list[str] = []
    for name in found:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def is_self_contained(source: str) -> bool:
    """True when the uploaded .tex is the whole design, not one file of a set."""
    if missing_dependencies(source):
        return False
    parts = split_document(_strip_comments(source))
    return parts is not None and bool(_ONLY_INPUTS.sub("", parts.body))


_BODY_STRAY = re.compile(
    r"\\documentclass[^\n]*\n|\\usepackage[^\n]*\n|\\begin\s*\{document\}|\\end\s*\{document\}"
)


def clean_latex_body(text: str) -> str:
    r"""Keep only what belongs between the document delimiters.

    Models routinely return the whole document even when asked for the body, so
    anything between ``\begin{document}`` and ``\end{document}`` is lifted out
    and the preamble lines they invented are dropped — the real preamble is
    already known and must not be duplicated.
    """
    out = _FENCE.sub("", (text or "").strip())
    begin = _BEGIN_DOC.search(out)
    if begin is not None:
        end = _END_DOC.search(out, begin.end())
        out = out[begin.end() : end.start()] if end else out[begin.end() :]
    return _BODY_STRAY.sub("", out).strip()


def clean_latex(text: str) -> str:
    """Strip anything the model wrapped around the document.

    Models fence LaTeX as often as they fence JSON, and some prepend a sentence
    before ``\\documentclass``. Both would be fed verbatim to the compiler and
    fail on line 1, so they are removed here rather than debugged there.
    """
    out = _FENCE.sub("", (text or "").strip())
    match = _DOC_START.search(out)
    if match and match.start() > 0:
        out = out[match.start() :]
    end = out.rfind(r"\end{document}")
    if end != -1:
        out = out[: end + len(r"\end{document}")]
    return out.strip()


#: A ``\usepackage`` naming a style file that lives beside the template rather
#: than in TeX Live. Only the main .tex is uploaded, so these never resolve.
_LOCAL_STYLE = re.compile(
    r"^[ \t]*\\usepackage(\[[^\]]*\])?\{(TLCresume|[A-Za-z]*resume|[A-Za-z]*cv)\}[ \t]*\n",
    re.M | re.I,
)
_INPUT_LINE = re.compile(r"^[ \t]*\\(input|include)\{[^}]*\}[ \t]*\n", re.M)
#: The same match as ``_LOCAL_STYLE`` but unanchored, for reporting rather than
#: substitution.
_LOCAL_STYLE_ANY = re.compile(
    r"\\usepackage(\[[^\]]*\])?\{(TLCresume|[A-Za-z]*resume|[A-Za-z]*cv)\}", re.I
)


def drop_missing_dependencies(latex: str) -> str:
    r"""Remove references to files that were never uploaded.

    Resume templates are commonly multi-file Overleaf projects, and only the
    main .tex reaches us. The prompt asks the model to inline what those files
    provided; this is the belt-and-braces pass for when it leaves the reference
    in anyway, because a stray ``\input{_header}`` fails the compile on line one
    and the operator just sees "PDF unavailable" with no idea why.
    """
    out = _INPUT_LINE.sub("", latex)
    return _LOCAL_STYLE.sub("", out)


def apply_presentation(preamble: str, content: ResumeContent) -> str:
    r"""Append the studio's look-and-feel overrides to a copied preamble.

    Inserted immediately before ``egin{document}`` so LaTeX's last-definition
    -wins gives them precedence over the template's own colours, fonts and
    alignment — without the template being edited or reproduced. A template
    that predates the hooks still compiles: every override redefines something
    guarded by ``\providecommand`` or simply re-declares a colour.
    """
    overrides = presentation.latex_overrides(content)
    if not overrides:
        return preamble
    match = _BEGIN_DOC.search(preamble)
    if match is None:
        return preamble + overrides
    return preamble[: match.start()] + overrides + preamble[match.start():]


def _candidate_block(content: ResumeContent) -> str:
    """The verified history, as compact JSON the model can quote from safely."""
    c = content.contact
    payload = {
        "name": c.full_name,
        "current_title": c.headline,
        "email": c.email,
        "phone": c.phone,
        "location": c.location,
        "summary": content.summary,
        "skills": [
            {"group": g.label, "tier": g.tier, "items": g.items} for g in content.skill_groups
        ],
        "experience": [
            {
                "company": e.company,
                "designation": e.designation,
                "from": e.start_label,
                "to": e.end_label,
                "location": e.location,
                "highlights": e.highlights,
            }
            for e in content.experience
        ],
        "projects": [
            {
                "name": p.name,
                # The employer this project was built at. Decided by the
                # composition (resume_composer._pair_projects_to_companies)
                # and NOT the model's to choose: it is what dates the project,
                # so moving it changes when the work happened. Without this
                # field the model paired projects to employers by guessing,
                # and shifted every one of them down by a job.
                "company": p.company,
                "description": p.description,
                "responsibilities": p.responsibilities,
                "achievements": p.achievements,
                "technologies": p.technologies,
            }
            for p in content.projects
        ],
        "education": [e.qualification for e in content.education],
        "certifications": content.certifications,
        # Sections the operator added in the studio. The template has no block
        # for these, so the model builds one in the template's own section
        # style — see the house rule below.
        "additional_sections": [
            {"title": s.title, "body": s.body, "items": s.items}
            for s in content.custom_sections
            if s.title.strip() and (s.body.strip() or s.items)
        ],
        # The rules the platform already applied. They are repeated here because
        # the model sees only this JSON and the template — without them it will
        # helpfully re-add the education section the template happens to define.
        "house_rules": {
            "education": "omitted - delete the template's education section entirely",
            "project_company_pairing": (
                "FIXED. Each project carries the company it was built at. If the template "
                "nests projects under employers, put each project under the employer named "
                "in its own 'company' field and nowhere else. If the template has a separate "
                "projects section, name that company on the project. Never reassign a "
                "project to a different employer: the employer is what dates it, and the "
                "dates have already been checked against when each technology was released."
            ),
            "timeline_bars": (
                "off - print plain date ranges, remove any bar, gantt strip or progress meter"
                if content.render_hints.get("timeline_bars") == "off"
                else "template default"
            ),
            "years_of_experience_to_present": content.render_hints.get("experience_years"),
            "dates": "the date labels above are final - print them verbatim, do not recompute",
            "project_dates": (
                "REMOVED - print no date, no dash and no placeholder next to any project"
                if content.render_hints.get("project_dates") == "off"
                else "print the date range each project gives"
            ),
            "null_dates": "a null or missing date means print nothing there, never invent one",
            "project_titles": (
                "a project prints under its employer's designation only - do not print a second, "
                "project-specific job title or role line beneath any project name"
            ),
            "presentation": presentation.house_rule(content)
            or "template default order and alignment",
            "additional_sections": (
                "print every entry in additional_sections as its own section, after the "
                "standard ones, using the template's own section heading style and list "
                "macros. These were added deliberately by the operator - never drop one."
            ),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _requirement_block(db: Session, requirement: Requirement | None) -> str:
    if requirement is None:
        return json.dumps({"note": "No requirement attached; present the history as-is."})

    mandatory, preferred = requirement_repository.mandatory_and_preferred(db, requirement.id)
    # Release dates travel with the JD's technologies so the model can honour the
    # timeline rule without guessing when something was invented.
    released = requirement_repository.first_release_dates(db, requirement.id)
    return json.dumps(
        {
            "role": requirement.parsed_role,
            "experience_required": requirement.min_experience_years,
            "location": requirement.location,
            "domain": requirement.domain,
            "mandatory_skills": mandatory,
            "preferred_skills": preferred,
            "responsibilities": requirement.responsibilities or [],
            "other_requirements": requirement.other_requirements or [],
            "technology_first_release_dates": released,
        },
        indent=2,
        ensure_ascii=False,
    )


#: A reply can come back empty or truncated for reasons that have nothing to do
#: with the prompt — a reasoning model swallowing its own answer, a token
#: ceiling, a bad sample. Retrying costs one call and turns an intermittent
#: failure into a non-event, which matters most on a template's first run.
_COMPOSE_ATTEMPTS = 3

#: Reasoning models spend most of their budget before writing anything, and they
#: routinely draft the whole document inside the <think> block first. Hit the
#: ceiling mid-thought and the reply contains no answer at all — which is what
#: "the model returned an empty body" actually meant. The ceiling has to cover
#: the thinking AND the document, not just the document.
_BODY_TOKENS = 32000
_DOCUMENT_TOKENS = 40000


def _log_bad_reply(kind: str, attempt: int, raw: str, cleaned: str) -> None:
    """Say what actually arrived. Without this the retry hides the real cause."""
    logger.warning(
        "LaTeX %s reply unusable (attempt %d/%d): %d raw chars, %d after cleaning. Head: %r",
        kind,
        attempt + 1,
        _COMPOSE_ATTEMPTS,
        len(raw or ""),
        len(cleaned or ""),
        (raw or "")[:300],
    )


def _call_model(user: str, system: str, *, max_tokens: int) -> tuple[str, str | None]:
    provider = get_llm_provider()

    def _call():
        return provider.chat(
            [{"role": "user", "content": user}],
            model=LLM_MODEL,
            system=system,
            temperature=LLM_TEMPERATURE,
            max_tokens=max_tokens,
            timeout=max(LLM_TIMEOUT_SECONDS, 300.0),
        )

    try:
        response = with_retry(_call, max_retries=2)
    except (LLMTransientError, LLMPermanentError) as exc:
        raise ValidationError(f"The LaTeX resume could not be composed: {exc}") from exc
    return response.text, response.model


def _assemble(preamble: str, body: str) -> str:
    return f"{preamble}\n{body}\n\\end{{document}}\n"


def assets_dir_for(template: ResumeTemplate) -> str | None:
    """The directory to compile this template beside, if it has one.

    An uploaded Overleaf project keeps its .cls, .sty and images on disk; the
    compile copies them in next to the .tex, exactly as Overleaf does. A
    template that arrived as a single self-contained file has none, and
    ``None`` means "nothing to copy".
    """
    directory = (getattr(template, "project_dir", None) or "").strip()
    if not directory:
        return None
    path = storage.absolute_path(directory)
    return str(path) if path.is_dir() else None


def render_blueprint(template: ResumeTemplate, content: ResumeContent) -> str:
    """Fill a templatized design with this resume. No model, no variance.

    This is the whole point of the blueprint pipeline: the design was turned
    into a Jinja template ONCE, at upload, and every resume after that is a
    render. The same content in the same template produces byte-identical
    LaTeX every time, in about a millisecond, where the model path took a
    minute and produced something slightly different on each run.

    The section list travels in the content (see ``content_window``), so
    removing Education or adding Publications in the studio changes what this
    prints without anybody touching the template.
    """
    content_window.seed(content, template)
    source = tb.render(template.jinja_source, tb.build_context(content, content_window.blueprint(content)))

    # Presentation overrides still apply: accent colour, typeface and header
    # alignment are the operator's, not the template's, and they are appended
    # to the preamble the same way on both paths.
    parts = split_document(source)
    if parts is None:
        return source
    return _assemble(apply_presentation(parts.preamble, content), parts.body)


def compose_latex(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    *,
    instructions: str | None = None,
) -> tuple[str, str | None]:
    r"""Return ``(latex_source, model)`` for this session's current content.

    The template is split at ``\begin{document}``. The preamble is copied
    verbatim — only its contact placeholders are substituted, in Python — and
    the model is asked for the body alone. That is what makes an arbitrary
    uploaded template work: the design is never retyped, so it cannot be
    mistyped.

    A template with no ``\begin{document}`` is a fragment with no preamble to
    protect, and falls back to whole-document composition.
    """
    template = template_repository.get(db, session.template_id)
    if template is None:
        raise ValidationError("This session has no template. Upload a .tex template and retry.")

    if (getattr(template, "jinja_source", None) or "").strip():
        # The design was templatized at upload. Filling it in is deterministic
        # and free, so there is nothing here for a model to do.
        return render_blueprint(template, content), None

    template_source = read_template_source(template)
    requirement = requirement_repository.get(db, session.requirement_id)
    candidate = _candidate_block(content)
    wanted = _requirement_block(db, requirement)
    extra = f"ADDITIONAL INSTRUCTIONS\n{instructions}\n\n" if instructions else ""

    parts = split_document(template_source)
    if parts is not None and not is_self_contained(template_source):
        # One file of a multi-file project: the macros and the section content
        # live in files that were never uploaded, so there is no preamble worth
        # protecting and no body worth mirroring. Reconstructing a whole
        # self-contained document is the only thing that can work, and the
        # operator is told at upload time that the design cannot be reproduced.
        missing = ", ".join(missing_dependencies(template_source))
        logger.info(
            "Template %s is not self-contained (missing: %s); composing a whole document",
            template.name,
            missing,
        )
        parts = None

    if parts is None:
        # Fragment or incomplete template: there is no preamble to protect, so
        # the whole-document prompt is the only option.
        user = LATEX_RESUME_USER.format(
            template_source=template_source,
            candidate=candidate,
            requirement=wanted,
            instructions=extra,
        )
        latex = ""
        model = None
        for attempt in range(_COMPOSE_ATTEMPTS):
            text, model = _call_model(
                user, LATEX_RESUME_SYSTEM, max_tokens=max(LLM_MAX_TOKENS, _DOCUMENT_TOKENS)
            )
            latex = drop_missing_dependencies(clean_latex(text))
            if r"\end{document}" in latex:
                break
            _log_bad_reply("whole document", attempt, text, latex)
        else:
            raise ValidationError(
                "The composed LaTeX is incomplete — it has no \\end{document}. "
                "This usually means the reply hit the token ceiling; try a shorter template."
            )
        return latex, model

    preamble = apply_presentation(
        drop_missing_dependencies(fill_preamble_placeholders(parts.preamble, content)), content
    )
    user = LATEX_BODY_USER.format(
        preamble=parts.preamble,
        template_body=parts.body,
        candidate=candidate,
        requirement=wanted,
        instructions=extra,
    )
    body = ""
    model = None
    for attempt in range(_COMPOSE_ATTEMPTS):
        text, model = _call_model(user, LATEX_BODY_SYSTEM, max_tokens=max(LLM_MAX_TOKENS, _BODY_TOKENS))
        body = clean_latex_body(text)
        if body:
            break
        _log_bad_reply("body", attempt, text, body)
    if not body:
        raise ValidationError(
            "The model returned an empty resume body. Try regenerating, or check the "
            "template has content between \\begin{document} and \\end{document}."
        )
    return _assemble(preamble, body), model


def repair_latex(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    broken_source: str,
    error_log: str,
) -> tuple[str, str | None] | None:
    r"""One attempt at fixing a body the LaTeX engine rejected.

    Returns ``None`` when there is nothing safe to repair — a fragment template,
    or a document whose preamble we do not control. With the preamble copied
    verbatim, a compile failure is almost always something in the body: an
    undefined macro, an unbalanced brace, or an unescaped ``&``. Handing the
    engine's own message back is the cheapest fix that actually works, and it is
    what makes a newly uploaded template survive its first run.
    """
    template = template_repository.get(db, session.template_id)
    if template is None:
        return None
    parts = split_document(read_template_source(template))
    if parts is None:
        return None

    broken = split_document(broken_source)
    if broken is None:
        return None

    requirement = requirement_repository.get(db, session.requirement_id)
    user = LATEX_BODY_USER.format(
        preamble=parts.preamble,
        template_body=parts.body,
        candidate=_candidate_block(content),
        requirement=_requirement_block(db, requirement),
        instructions=LATEX_BODY_REPAIR.format(
            error=(error_log or "no log captured")[:2000],
            broken=broken.body[:12000],
        )
        + "\n\n",
    )
    try:
        text, model = _call_model(user, LATEX_BODY_SYSTEM, max_tokens=max(LLM_MAX_TOKENS, _BODY_TOKENS))
    except ValidationError:
        return None

    body = clean_latex_body(text)
    if not body:
        return None
    preamble = apply_presentation(
        drop_missing_dependencies(fill_preamble_placeholders(parts.preamble, content)), content
    )
    return _assemble(preamble, body), model
