"""Was this claim even possible in the year it is claimed for?

A resume composed for a JD asking "7+ years of AI Engineering" has to put the
candidate somewhere in 2019. But in 2019 there was no LangGraph, no GPT-4, no
RAG pipeline, and "AI Engineer" was not yet a title anybody printed on a
business card. A resume that says otherwise is not aggressive framing, it is a
provable lie — the reader can date the technology themselves — and it is the
single most expensive kind of error this platform can make.

Two rules, both derived from dates that are already written down:

1. **Technology cannot predate its release.** Each employment carries a period
   (``ground_rules.meet_jd_experience`` lays those out), each project inherits
   the period of the employment it sits under, and each technology carries a
   ``first_release_date`` on the master. Comparing them is arithmetic.
2. **A job title cannot predate the job.** "AI Engineer" in 2019 is the same
   class of error, so the earlier roles step back down a ladder to whatever
   that era's honest equivalent was — Software Engineer, then Backend
   Engineer, then Machine Learning Engineer, then AI Engineer.

Both run twice: as constraints in the composer prompt, so the wrong thing is
not written in the first place, and as validation rules over the finished
document, so anything the model wrote anyway is caught before approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.technology import Technology
from app.schemas.resume_content import ResumeContent

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    from app.services.role_timeline import Timeline as RoleTimeline

#: Terminology that is not a row in the technology master but still dates a
#: sentence hard. Kept here rather than in the validation engine so the
#: composer prompt and the validation rule read from one list.
CONCEPT_FIRST_USED: dict[str, int] = {
    "llm": 2020,
    "large language model": 2020,
    "gpt-3": 2020,
    "gpt-4": 2023,
    "gpt-4o": 2024,
    "chatgpt": 2022,
    "rag": 2021,
    "retrieval augmented generation": 2021,
    "retrieval-augmented generation": 2021,
    "vector database": 2021,
    "vector search": 2020,
    "semantic search": 2019,
    "embeddings": 2018,
    "prompt engineering": 2022,
    "generative ai": 2022,
    "genai": 2022,
    "agentic": 2023,
    "ai agent": 2023,
    "ai agents": 2023,
    "multi-agent": 2023,
    "tool calling": 2023,
    "function calling": 2023,
    "guardrails": 2023,
    "llm-as-a-judge": 2023,
    "hallucination": 2022,
    "groundedness": 2023,
    "transformer": 2018,
    "bert": 2018,
    "microservices": 2014,
    "kubernetes": 2015,
    "serverless": 2015,
    "infrastructure as code": 2014,
    "ci/cd": 2011,
    "rest api": 2005,
}

# ─────────────────────────────────────────────────────────── job titles
#
# What a title was called before it existed is no longer answered here. It used
# to be two hand-written tables — every job title mapped to a year, and a
# ladder of earlier equivalents per discipline — which knew only the roles
# somebody had thought to add and could not see how a given advert words its
# own role. ``role_timeline.resolve`` derives it for the role in front of us
# and stores the answer, so this module asks rather than remembers.


# ────────────────────────────────────────────────── technology release dates
def technology_release_years(db: Session) -> dict[str, int]:
    """``{lower-cased name or alias: release year}`` for everything datable.

    The master's ``first_release_date`` is the authority; the concept list
    above fills in the vocabulary that has no row of its own ("RAG", "agentic")
    but dates a sentence just as hard.
    """
    years: dict[str, int] = dict(CONCEPT_FIRST_USED)
    for tech in db.query(Technology).filter(Technology.first_release_date.isnot(None)).all():
        year = tech.first_release_date.year
        if tech.name:
            years[tech.name.strip().lower()] = year
        for alias in tech.aliases or []:
            years.setdefault(str(alias).strip().lower(), year)
    return years


def _scanner(names: list[str]) -> re.Pattern[str] | None:
    """One alternation, longest name first so "Next.js" wins over "Next"."""
    usable = [n for n in names if len(n) > 1]
    if not usable:
        return None
    ordered = sorted(usable, key=len, reverse=True)
    return re.compile(
        r"(?<![A-Za-z0-9+#.])(" + "|".join(re.escape(n) for n in ordered) + r")(?![A-Za-z0-9+#])",
        re.IGNORECASE,
    )


@dataclass(slots=True)
class Anachronism:
    """A named thing that had not been released when it is claimed."""

    term: str
    released: int
    claimed_year: int


def anachronisms_in(text: str, year: int | None, releases: dict[str, int]) -> list[Anachronism]:
    """Everything in ``text`` that did not exist yet in ``year``."""
    if not text or year is None:
        return []
    pattern = _scanner(list(releases))
    if pattern is None:
        return []
    found: dict[str, Anachronism] = {}
    for match in pattern.finditer(text):
        term = match.group(1)
        released = releases.get(term.strip().lower())
        if released is not None and released > year:
            found.setdefault(term.lower(), Anachronism(term=term, released=released, claimed_year=year))
    return list(found.values())


def allowed_terms_for(year: int | None, releases: dict[str, int]) -> tuple[list[str], list[str]]:
    """``(available, not_yet)`` split of every datable term at ``year``."""
    if year is None:
        return sorted(releases), []
    available = sorted(name for name, released in releases.items() if released <= year)
    not_yet = sorted(name for name, released in releases.items() if released > year)
    return available, not_yet


# ─────────────────────────────────────────────────────────── period lookup
_YEAR = re.compile(r"(19|20)\d{2}")


def start_year_of(label: str | None) -> int | None:
    match = _YEAR.search(label or "")
    return int(match.group(0)) if match else None


def end_year_of(label: str | None) -> int:
    """The year a period closed. "Present" (or a blank) means it has not.

    Content is judged against this rather than the start year: a job running
    2023–2025 may legitimately describe something released in 2024, and
    flagging that would block a correct resume. Only the job TITLE is judged
    against the start, because the resume prints one title for the whole run.
    """
    year = start_year_of(label)
    return year if year is not None else date.today().year


@dataclass(slots=True)
class Period:
    """One employment's window, plus what it may honestly say."""

    company: str
    start_year: int | None
    end_label: str | None
    honest_role: str | None = None
    forbidden: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)


def periods_for(
    content: ResumeContent,
    timeline: RoleTimeline | None,
    releases: dict[str, int],
) -> list[Period]:
    """One :class:`Period` per employment, most recent first.

    Built after the timeline has been laid out and before the model is asked to
    write anything, so the prompt can state — per employer — which title is
    honest for that window and which technologies had not shipped yet. The
    ``forbidden`` list is capped: it is a warning shot for the model, not an
    exhaustive index, and the validation pass catches whatever slips.
    """
    periods: list[Period] = []
    for exp in content.experience:
        year = start_year_of(exp.start_label)
        _, not_yet = allowed_terms_for(year, releases)
        periods.append(
            Period(
                company=exp.company,
                start_year=year,
                end_label=exp.end_label,
                honest_role=timeline.title_for(year) if timeline else None,
                # Only what the JD's own world would tempt the model toward:
                # the newest things, which are exactly the ones it reaches for.
                forbidden=sorted(not_yet, key=lambda n: -releases[n])[:18],
                projects=[
                    p.name
                    for p in content.projects
                    if p.source_company_id and p.source_company_id == exp.source_company_id
                ],
            )
        )
    return periods


def timeline_brief(periods: list[Period]) -> str:
    """The per-employer era block handed to the composer."""
    if not periods:
        return "(no employment on this resume)"
    lines: list[str] = []
    for period in periods:
        window = f"{period.start_year or '?'}–{period.end_label or 'Present'}"
        lines.append(f"- {period.company} ({window})")
        if period.honest_role:
            lines.append(f"    designation MUST be: {period.honest_role}")
        if period.projects:
            lines.append(f"    projects under it: {', '.join(period.projects)}")
        if period.forbidden:
            lines.append(
                f"    DID NOT EXIST YET, never mention here: {', '.join(period.forbidden)}"
            )
    return "\n".join(lines)
