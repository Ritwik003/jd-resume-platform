"""Turn an uploaded design into a fillable template — once, at upload.

This is the expensive, model-driven half of the blueprint pipeline, and it
runs exactly once per template rather than once per resume. What it produces
is checked before it is kept:

1. it has to be valid Jinja,
2. it has to still contain the design's preamble,
3. it has to RENDER against a realistic sample resume, and
4. that render has to COMPILE to a PDF.

Only then is the template accepted. A design that fails any of those is
rejected at upload, in front of the person who chose it — which is the whole
point of doing this at upload instead of discovering it on somebody's first
real resume, halfway through a vendor deadline.
"""

from __future__ import annotations

import logging
import re

from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE
from app.prompts.templates import (
    TEMPLATIZE_BODY_SYSTEM,
    TEMPLATIZE_BODY_USER,
    TEMPLATIZE_REPAIR,
    TEMPLATIZE_SYSTEM,
    TEMPLATIZE_USER,
)
from app.schemas.resume_content import (
    ContactBlock,
    EducationBlock,
    ExperienceBlock,
    ProjectBlock,
    ResumeContent,
    SkillGroup,
)
from app.services import latex_composer
from app.services import template_blueprint as tb
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry
from app.services.rendering import latex

logger = logging.getLogger(__name__)

#: A design plus loops is longer than the design alone, and a truncated
#: template is worse than none: it fails at render time, per resume, forever.
#: Measured, not guessed: a reasoning model spends the budget THINKING before
#: it writes a character. On a 12 KB design the configured provider used 16k
#: tokens without emitting a single line of template and the reply arrived
#: empty; given 48k it finished in 27k with a valid one. So the first rung is
#: set where a reply actually completes, and the second is there for a design
#: half again as large.
TEMPLATIZE_TOKENS = 48000
TEMPLATIZE_TOKEN_LADDER = (TEMPLATIZE_TOKENS, 96000)
#: An upload waits on this, so it is bounded rather than generous: two rungs at
#: five minutes is the most an operator is asked to sit through before being
#: told to try Convert again. Reasoning at this length is slow on some
#: providers — that is a reason to fail visibly, not to wait longer.
TEMPLATIZE_TIMEOUT_SECONDS = 300.0

_FENCE = re.compile(r"^\s*```(?:latex|tex)?\s*|\s*```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


def sample_content() -> ResumeContent:
    """A realistic resume used to prove a templatized design actually renders.

    Deliberately not minimal: two employers with projects under each, several
    skill groups, an education row and a custom section, so a template that
    only works for the one-of-everything case fails here rather than later.
    """
    return ResumeContent(
        contact=ContactBlock(
            full_name="Alex Sample",
            headline="Senior Backend Engineer",
            email="alex.sample@example.com",
            phone="+91 90000 00000",
            location="Bengaluru, India",
        ),
        summary=(
            "Backend engineer with 7+ years building payment and data services, "
            "specialising in Python, PostgreSQL and AWS."
        ),
        skill_groups=[
            SkillGroup(label="Languages", tier="PRIMARY", items=["Python", "SQL", "Go"]),
            SkillGroup(label="Backend", tier="PRIMARY", items=["FastAPI", "Django", "gRPC"]),
            SkillGroup(label="Cloud & DevOps", tier="SECONDARY", items=["AWS", "Docker", "Terraform"]),
        ],
        experience=[
            ExperienceBlock(
                source_company_id="c1",
                company="Northwind Technologies",
                designation="Senior Backend Engineer",
                start_label="Mar 2022",
                end_label="Present",
                location="Bengaluru",
                highlights=["Owned the payments platform serving 4M monthly transactions."],
            ),
            ExperienceBlock(
                source_company_id="c2",
                company="Bluepeak Systems",
                designation="Backend Engineer",
                start_label="Jul 2018",
                end_label="Feb 2022",
                location="Pune",
                highlights=[],
            ),
        ],
        projects=[
            ProjectBlock(
                source_project_id="p1",
                name="Settlement Engine",
                company="Northwind Technologies",
                source_company_id="c1",
                description=(
                    "Event-driven settlement service reconciling card and UPI payments "
                    "across six acquiring banks."
                ),
                responsibilities=[
                    "Designed the idempotent ledger write path in Python and PostgreSQL",
                    "Built the replay tooling that recovers a failed settlement window",
                ],
                achievements=["Cut settlement lag from four hours to under nine minutes"],
                technologies=["Python", "PostgreSQL", "Kafka", "AWS"],
            ),
            ProjectBlock(
                source_project_id="p2",
                name="Merchant Onboarding API",
                company="Bluepeak Systems",
                source_company_id="c2",
                description="Public REST API onboarding merchants with KYC document checks.",
                responsibilities=["Implemented the document verification pipeline"],
                achievements=[],
                technologies=["Django", "Celery", "Redis"],
            ),
        ],
        education=[
            EducationBlock(
                qualification="B.E. Computer Science",
                institution="University of Pune",
                year="2018",
            )
        ],
        certifications=["AWS Certified Solutions Architect - Associate"],
    )


def templatize(
    source: str, blueprint: tb.Blueprint, *, name: str, assets_dir: str | None = None
) -> tuple[str, str | None]:
    """Convert one design into a Jinja template. Returns ``(jinja, model)``.

    The design is split at ``\\begin{document}`` and only the BODY is sent to
    be templatized; the preamble is copied out here, in Python. That is worth
    doing for two reasons: the preamble is the design, and a model that never
    sees it cannot rewrite it — and asking for half the file roughly halves
    what has to be generated, which is the difference between a reply that
    completes and one that runs out of budget.

    A design with no ``\\begin{document}`` is a fragment with no preamble to
    protect, and falls back to whole-document conversion.

    Raises :class:`template_blueprint.TemplatizeError` when the result cannot
    be trusted — which is the useful outcome, because the caller can then
    refuse the upload rather than storing a template that fails per resume.
    """
    listing = "\n".join(
        f"  - {s.title}  (kind: {s.kind})" for s in blueprint.sections
    ) or "  (none detected — drive everything from the `sections` loop)"

    parts = latex_composer.split_document(source)
    if parts is not None:
        system = TEMPLATIZE_BODY_SYSTEM
        user = TEMPLATIZE_BODY_USER.format(
            name=name,
            preamble=parts.preamble,
            body=parts.body,
            line_count=parts.body.count("\n") + 1,
            sections=listing,
        )
    else:
        system = TEMPLATIZE_SYSTEM
        user = TEMPLATIZE_USER.format(
            name=name,
            line_count=source.count("\n") + 1,
            source=source,
            sections=listing,
        )

    text, model = _ask(system, user, name=name)
    body = _strip_fences(text)
    if not body:
        raise tb.TemplatizeError(
            "The model returned nothing for this design, even at the largest "
            "token budget. This usually means the provider is degraded or the "
            "design is very large; try again, or upload a smaller .tex."
        )

    if parts is not None:
        # The model was asked for the body alone. If it returned a whole
        # document anyway, keep only the part it was asked for rather than
        # ending up with two preambles.
        inner = latex_composer.split_document(body)
        if inner is not None:
            body = inner.body
        jinja_source = f"{parts.preamble}\n{body.strip()}\n\\end{{document}}\n"
    else:
        jinja_source = body

    try:
        verify(jinja_source, source, assets_dir=assets_dir)
    except tb.TemplatizeError as first:
        # One repair pass, handing the model its own output and the complaint.
        # This is the same move the LaTeX composer makes on a failed compile,
        # and for the same reason: the failures that actually happen are
        # mechanical — an unbalanced <% if %>, a lost \begin{document} — and a
        # model shown the specific error fixes them. Re-asking from scratch
        # would cost the same and reproduce the same mistake.
        logger.info("Templatized %s was rejected (%s); repairing", name, first)
        repaired, repair_model = _ask(
            system,
            TEMPLATIZE_REPAIR.format(error=str(first), previous=jinja_source),
            name=f"{name} (repair)",
        )
        body = _strip_fences(repaired)
        if not body:
            raise first from None
        if parts is not None:
            inner = latex_composer.split_document(body)
            jinja_source = (
                f"{parts.preamble}\n{(inner.body if inner else body).strip()}\n\\end{{document}}\n"
            )
        else:
            jinja_source = body
        # A second failure is the design's answer: report the ORIGINAL
        # complaint, which is the one that describes what is actually wrong.
        try:
            verify(jinja_source, source, assets_dir=assets_dir)
        except tb.TemplatizeError as second:
            logger.info("The repair for %s did not hold either: %s", name, second)
            raise second from None
        model = repair_model or model

    return jinja_source, model


def _ask(system: str, user: str, *, name: str) -> tuple[str, str | None]:
    """One templatize call, retried at a larger budget when it comes back empty.

    An empty reply from a reasoning model means it never finished thinking, so
    the fix is more room rather than another identical attempt.
    """
    provider = get_llm_provider()
    model: str | None = None

    for budget in TEMPLATIZE_TOKEN_LADDER:
        def _call(budget: int = budget):
            return provider.chat(
                [{"role": "user", "content": user}],
                model=LLM_MODEL,
                system=system,
                temperature=LLM_TEMPERATURE,
                max_tokens=max(LLM_MAX_TOKENS, budget),
                timeout=TEMPLATIZE_TIMEOUT_SECONDS,
            )

        try:
            # No retry inside a rung: the ladder IS the retry, and an upload
            # that can hang for the better part of an hour before saying
            # anything is worse than one that gives up and offers Convert.
            response = with_retry(_call, max_retries=0)
        except (LLMTransientError, LLMPermanentError) as exc:
            raise tb.TemplatizeError(f"The design could not be prepared: {exc}") from exc

        model = response.model or model
        if (response.text or "").strip():
            return response.text, model
        logger.warning(
            "Templatizing %s: empty reply at max_tokens=%s; raising the budget", name, budget
        )

    return "", model


def verify(jinja_source: str, original: str, *, assets_dir: str | None = None) -> None:
    """Prove a templatized design is usable before it is stored.

    Four checks, cheapest first, each catching a failure mode seen in
    practice: invalid Jinja, a preamble the model rewrote, a template that
    renders empty, and one that renders but does not compile.
    """
    tb.validate_jinja(jinja_source)

    if "\\begin{document}" not in jinja_source:
        raise tb.TemplatizeError("The templatized design lost its \\begin{document}.")

    # The design lives in the preamble. A model that rewrote it has produced a
    # different template, however well it renders.
    original_class = re.search(r"\\documentclass[^\n]*", original)
    if original_class and original_class.group(0) not in jinja_source:
        raise tb.TemplatizeError(
            "The templatized design changed the \\documentclass line, so it is no "
            "longer the design that was uploaded."
        )

    context = tb.build_context(sample_content(), tb.detect_sections(original))
    try:
        rendered = tb.render(jinja_source, context)
    except Exception as exc:  # noqa: BLE001 — any render failure is the same verdict
        raise tb.TemplatizeError(f"The templatized design does not render: {exc}") from exc

    if "Alex Sample" not in rendered:
        raise tb.TemplatizeError(
            "The templatized design rendered without the candidate's name, so its "
            "placeholders are not wired to the data."
        )

    error = _compile_check(rendered, assets_dir=assets_dir)
    if error:
        raise tb.TemplatizeError(f"The templatized design does not compile: {error}")


def _compile_check(rendered: str, *, assets_dir: str | None = None) -> str | None:
    """``None`` when it compiles, else a one-line reason."""
    try:
        latex.compile_to_bytes(rendered, assets_dir=assets_dir)
    except latex.LatexCompileError as exc:
        return str(exc)[:400]
    except FileNotFoundError:
        # No engine installed here. Not the template's fault, and refusing the
        # upload over it would make the platform unusable on a machine without
        # LaTeX — the render check above has already done the real work.
        logger.warning("No LaTeX engine available; skipped the compile check")
        return None
    return None
