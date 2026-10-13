"""The professional summary, written rather than assembled.

It used to be pure string interpolation over ``summary_templates.py`` — six
f-string shapes filled with the role, the years, three skill names and one
bullet lifted from somewhere further down the page. That guaranteed it could
never hallucinate, which was the point, but it also guaranteed it could never
be good: it produced things like

    "Senior AI Engineer with a 7-year track record, working across Fabric, MCP
    frameworks, and Microsoft 365 Copilot. Cut per-record processing time by
    profiling the pipeline end-to-end and removing throttling bottlenecks."

— a label, a tech list, and a sentence about profiling a pipeline that a
reader has already met verbatim in a project bullet. The paragraph most
readers judge the page by was the one part of the resume nobody was writing.

So the model writes it, against the structure that actually works — role and
level, specialisation, one concrete achievement, what it adds up to for this
job — and against the whole resume rather than one bullet. The grounding that
the templates gave away for free is bought back explicitly: the prompt is
given only what the resume already says, is told which JD skills the candidate
does NOT have, and the result is checked before it is used.

``summary_templates`` stays as the fallback. A summary is not worth failing a
composition over, and an assembled one is still true.
"""

from __future__ import annotations

import logging
import re

from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS
from app.prompts.templates import SUMMARY_SYSTEM, SUMMARY_USER
from app.schemas.resume_content import ResumeContent
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry

logger = logging.getLogger(__name__)

#: Long enough to be a summary, short enough to be read. The upper bound is
#: also what stops the model from writing a second resume.
MIN_CHARS, MAX_CHARS = 120, 1200

#: The words that mean a summary gave up and described a personality instead
#: of a career. Cheap to check and worth checking: a model that reaches for
#: one of these has stopped using the resume.
_FILLER = re.compile(
    r"\b(?:passionate|seasoned|results[- ]driven|dynamic professional|"
    r"detail[- ]oriented|team player|hard[- ]working|self[- ]starter|"
    r"go[- ]getter|thought leader|synerg\w+|proven track record)\b",
    re.IGNORECASE,
)
#: A summary is written about the candidate, not by them.
_FIRST_PERSON = re.compile(r"(?:^|[\s\"'(])(?:I|I'm|I've|my|me)\b")
_FENCE = re.compile(r"^\s*```[a-z]*\s*|\s*```\s*$", re.MULTILINE)


def _clean(text: str) -> str:
    """Strip the wrappers a model adds around a paragraph it was asked for."""
    body = _FENCE.sub("", text or "").strip()
    # A model asked for prose sometimes returns it quoted, or under a heading
    # it was told not to write.
    body = re.sub(r"^(?:professional\s+)?summary\s*[:\-–]\s*", "", body, flags=re.IGNORECASE)
    if len(body) > 1 and body[0] in "\"'" and body[-1] == body[0]:
        body = body[1:-1].strip()
    return re.sub(r"\s+", " ", body).strip()


def _rejects(summary: str, forbidden: list[str]) -> str | None:
    """Why this summary cannot be used, or ``None`` if it can.

    Checked rather than trusted, because the two failures that matter are
    both silent: a technology the candidate does not have reads perfectly, and
    so does a sentence of pure adjective.
    """
    if not MIN_CHARS <= len(summary) <= MAX_CHARS:
        return f"length {len(summary)} outside {MIN_CHARS}-{MAX_CHARS}"
    if _FIRST_PERSON.search(summary):
        return "written in the first person"
    match = _FILLER.search(summary)
    if match:
        return f"filler adjective {match.group(0)!r}"
    lowered = summary.lower()
    for skill in forbidden:
        name = skill.strip().lower()
        # Word-boundary, so "Go" does not match "going" and "R" matches nothing.
        if len(name) > 2 and re.search(rf"(?<![a-z0-9+#]){re.escape(name)}(?![a-z0-9+#])", lowered):
            return f"claims {skill!r}, which the candidate does not have"
    return None


def _projects_brief(content: ResumeContent, limit: int = 4) -> str:
    lines: list[str] = []
    for project in content.projects[:limit]:
        bullets = (project.achievements or []) + (project.responsibilities or [])
        lines.append(f"- {project.name} ({project.company or 'no employer'}): {project.description}")
        for bullet in bullets[:3]:
            lines.append(f"    * {bullet}")
    return "\n".join(lines) or "- (no projects on this resume)"


def write(
    content: ResumeContent,
    *,
    role: str,
    years: float | None,
    responsibilities: list[str] | None = None,
    jd_skills: list[str] | None = None,
    missing_skills: list[str] | None = None,
    direction: str | None = None,
) -> tuple[str | None, str | None]:
    """``(summary, model)`` for this resume, or ``(None, None)`` to fall back.

    ``direction`` is what the operator asked for in the chat — "shorter",
    "lead with the Azure work", "less jargon". It is passed through verbatim
    and outranks the house defaults, because the person typing it is looking
    at the client and at the resume and this module is looking at neither.
    What it cannot override is the grounding: no unsupported claim, no
    invented number, no technology the candidate does not have.

    Never raises. A summary is the one part of a resume that can be assembled
    from the facts if the model is unreachable, so failing here costs a
    slightly duller paragraph rather than a composition.
    """
    forbidden = [s for s in (missing_skills or []) if s and s.strip()]
    asked = (direction or "").strip()
    instruction = (
        f"WHAT THE OPERATOR ASKED FOR — follow this:\n{asked}\n\n" if asked else ""
    )
    user = SUMMARY_USER.format(
        role=role or "n/a",
        years=f"{years:g}+ years" if years else "not stated",
        responsibilities="; ".join((responsibilities or [])[:6]) or "n/a",
        jd_skills=", ".join(jd_skills or []) or "n/a",
        missing_skills=", ".join(forbidden) or "none — the candidate has them all",
        current_title=(content.experience[0].designation if content.experience else "") or "n/a",
        companies=", ".join(e.company for e in content.experience if e.company) or "n/a",
        technologies=", ".join(content.all_technologies()[:40]) or "n/a",
        projects=_projects_brief(content),
        direction=instruction,
    )

    try:
        provider = get_llm_provider()
        response = with_retry(
            lambda: provider.chat(
                [{"role": "user", "content": user}],
                model=LLM_MODEL,
                system=SUMMARY_SYSTEM,
                temperature=LLM_TEMPERATURE,
                max_tokens=max(LLM_MAX_TOKENS, 16000),
                timeout=LLM_TIMEOUT_SECONDS,
            ),
            max_retries=1,
        )
    except (LLMTransientError, LLMPermanentError) as exc:
        logger.warning("Could not write the summary: %s", exc)
        return None, None
    except Exception:  # noqa: BLE001 — the summary must never sink a compose
        logger.exception("Writing the summary failed")
        return None, None

    summary = _clean(response.text or "")
    reason = _rejects(summary, forbidden)
    if reason:
        logger.warning("Rejected the written summary (%s): %r", reason, summary[:160])
        return None, None
    return summary, response.model
