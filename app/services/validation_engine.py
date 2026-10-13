"""Sections 24/25 — the validation assistant.

Design rule from section 25: every finding here is a warning with
[Modify] / [Keep] / [Dismiss], never a silent rejection — the user always
decides what a claim means. What changed is what happens next: approval
(``resume_session_service.approve``) now refuses to proceed while any warning
on the current version is still OPEN. The decision is still the user's; it
just has to be an actual decision before the resume can leave the studio.

**Only facts are warned about.** With ``VALIDATION_STRICT_FACTS_ONLY`` on (the
default) a handful of rules run, and each compares two things that are both
written down somewhere:

    1. a skill name that matches nothing in the technology master
    2. the experience the resume's dates assert versus the dates on record
    3. a JD-mandatory skill the resume cannot evidence
    4. a JD-mandatory skill that matches no technology row at all
    5. no project or company in the bank directly matches the JD's role

Plus two LLM-judged passes, both optional and non-fatal: the groundedness
judge (moulded text against the original it was moulded from) and, at
approval only, a language/grammar/spelling/consistency pass over the final
copy regardless of how it got there — moulding, a manual edit, or the chat
agent.

Projects carry no date of their own (see ``app/schemas/resume_content.py``),
so there is nothing left to check a technology's release date against there —
that rule is gone rather than kept dark.

Everything the composition deliberately does — reframing a project, taking the
JD's job title, expanding a timeline to meet the JD — is an operator decision,
not a defect. Warning about those taught people to click through the warnings
that mattered, so they no longer fire. The older rules are kept below and come
back when the flag is turned off.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import (
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
    VALIDATION_ENABLED,
    VALIDATION_STRICT_FACTS_ONLY,
)
from app.models.company import Company
from app.models.person import Employment
from app.models.requirement import Requirement, RequirementTechnology
from app.models.resume_session import ResumeCompositionSession
from app.models.technology import PersonTechnology, Technology
from app.prompts.templates import FINAL_REVIEW_SYSTEM, VALIDATION_SYSTEM
from app.schemas.resume_content import ResumeContent
from app.services import era, ground_rules, jd_fit, role_timeline
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.utils.enums import ValidationSeverity, ValidationWarningType
from app.utils.helper import parse_iso_date

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Finding:
    warning_type: str
    severity: str
    message: str
    content_path: str | None = None
    claim_text: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


#: Terminology that did not exist (or was not in common use) before a given year.
#: Section 25: "potentially anachronistic terminology".
ANACHRONISTIC_TERMS: dict[str, int] = {
    "llm": 2020,
    "large language model": 2020,
    "gpt-3": 2020,
    "gpt-4": 2023,
    "chatgpt": 2022,
    "rag": 2021,
    "retrieval augmented generation": 2021,
    "retrieval-augmented generation": 2021,
    "langchain": 2022,
    "llamaindex": 2022,
    "vector database": 2021,
    "prompt engineering": 2022,
    "generative ai": 2022,
    "genai": 2022,
    "agentic": 2023,
    "transformer": 2018,
    "bert": 2018,
    "kubernetes": 2015,
    "docker": 2013,
    "terraform": 2014,
    "fastapi": 2019,
    "react hooks": 2019,
    "pytorch": 2017,
    "tensorflow": 2016,
    "spark": 2014,
    "kafka": 2012,
}


def _year_of(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", value)
    return int(m.group(0)) if m else None


# ------------------------------------------------------------------ rule set
def _check_anachronisms(content: ResumeContent) -> list[Finding]:
    """Terminology that predates its own era.

    A project carries no date of its own (see ``app/schemas/resume_content.py``),
    so the only period a project's text can be checked against is a year it
    states inline — there is nothing else to anchor it to.
    """
    findings: list[Finding] = []
    for path, text in content.text_blocks():
        lowered = text.lower()
        reference_year = _year_of(text)
        if reference_year is None:
            continue

        for term, introduced in ANACHRONISTIC_TERMS.items():
            if term in lowered and reference_year < introduced:
                findings.append(
                    Finding(
                        warning_type=ValidationWarningType.ANACHRONISTIC_TERMINOLOGY,
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"'{term}' is referenced against {reference_year}, but it was not in "
                            f"general use until around {introduced}. Review whether this statement "
                            f"matches the period described."
                        ),
                        content_path=path,
                        claim_text=text[:500],
                        evidence={"term": term, "referenced_year": reference_year, "introduced": introduced},
                    )
                )
    return findings


def _check_unknown_technologies(db: Session, content: ResumeContent) -> list[Finding]:
    """A skill name that matches nothing real.

    Checked against the technology master by name and by alias, so "Postgres"
    and "postgresql" both resolve. What this catches is the typo and the
    invented product name — "Kubernets", "AWS Lambda Functions Pro" — which are
    the only skill-name problems a reader can verify without knowing the
    candidate.

    It deliberately does NOT check whether the person has the skill. Moulding a
    resume toward a JD legitimately surfaces skills the stored profile never
    flagged, and warning on those made every generated resume arrive with a
    dozen warnings nobody read.
    """
    known: set[str] = set()
    for tech in db.query(Technology).all():
        if tech.name:
            known.add(tech.name.strip().lower())
        for alias in tech.aliases or []:
            known.add(str(alias).strip().lower())
    if not known:
        # An empty master cannot tell a real name from a typo; saying nothing is
        # better than flagging every skill on the resume.
        return []

    findings: list[Finding] = []
    seen: set[str] = set()
    for group_index, group in enumerate(content.skill_groups):
        for item in group.items:
            name = item.strip()
            key = name.lower()
            if not name or key in known or key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    warning_type=ValidationWarningType.UNKNOWN_TECHNOLOGY,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"'{name}' does not match any technology on record. Check the spelling, "
                        f"or add it to the technology master if it is real."
                    ),
                    content_path=f"skill_groups[{group_index}].items",
                    claim_text=name,
                    evidence={"technology": name},
                )
            )

    for index, project in enumerate(content.projects):
        for item in project.technologies:
            name = item.strip()
            key = name.lower()
            if not name or key in known or key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    warning_type=ValidationWarningType.UNKNOWN_TECHNOLOGY,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"'{name}' is listed on '{project.name}' but matches no technology on "
                        f"record. Check the spelling, or add it to the technology master."
                    ),
                    content_path=f"projects[{index}].technologies",
                    claim_text=name,
                    evidence={"technology": name, "project": project.name},
                )
            )
    return findings


def _check_employment_timeline(db: Session, session: ResumeCompositionSession) -> list[Finding]:
    """Retired: employment rows carry no dates to overlap.

    Nothing about a person, company or project is dated in the bank any more —
    the timeline is composed per resume from the JD's own years. There are no
    longer two sets of dates that could disagree, so there is nothing here to
    check. Kept as a no-op because the non-strict rule set still references it.
    """
    return []


def _retired_check_employment_timeline(db: Session, session: ResumeCompositionSession) -> list[Finding]:
    """The original implementation, unreachable — see above."""
    findings: list[Finding] = []
    employments = (
        db.query(Employment).filter(Employment.person_id == session.person_id).all()
    )
    parsed = []
    for employment in employments:
        start = parse_iso_date(employment.start_date)
        end = parse_iso_date(employment.end_date)
        company = db.query(Company).filter(Company.id == employment.company_id).first()
        parsed.append((employment, start, end, company.name if company else "Unknown"))

    ordered = sorted([p for p in parsed if p[1]], key=lambda p: p[1])
    for i in range(len(ordered) - 1):
        _, _, end_a, name_a = ordered[i]
        _, start_b, _, name_b = ordered[i + 1]
        if end_a and start_b and start_b < end_a:
            findings.append(
                Finding(
                    warning_type=ValidationWarningType.OVERLAPPING_EMPLOYMENT,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"Employment at {name_a} (to {end_a}) overlaps {name_b} (from {start_b}). "
                        f"Confirm the dates are correct before sending."
                    ),
                    evidence={"company_a": name_a, "end_a": str(end_a), "company_b": name_b, "start_b": str(start_b)},
                )
            )
    return findings


def _check_experience_claim(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[Finding]:
    """What the resume says about years, against what the dates say.

    No career total is stored, so both halves of this comparison come from the
    document itself: the span the selected employment really covered, and the
    span it covers now that the JD rule has stretched it. Two written-down
    numbers, exactly like the other strict rules.

    Two shapes, one warning type:

    * the timeline was expanded to meet the JD (the usual case) — say by how
      much, so whoever submits it knows what is being asserted
    * the resume is still short of what the JD asks — say that too

    Neither blocks approval.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()

    wanted = requirement.min_experience_years if requirement else None
    shown = content.render_hints.get("experience_years")
    before = content.render_hints.get("experience_years_before")

    if shown and (before is None or float(shown) > float(before) + 0.5):
        # ``before`` is None when the selected employment rows carried no dates
        # at all — an autoselected session, before anyone typed any. The resume
        # still asserts a span, and that it was composed rather than recorded is
        # the more important half of the warning, not a reason to stay silent.
        held = (
            f"The employment dates on record span {float(before):g}."
            if before is not None
            else "The employment rows on record carry no dates of their own."
        )
        return [
            Finding(
                warning_type=ValidationWarningType.EXPERIENCE_BELOW_REQUIREMENT,
                severity=ValidationSeverity.WARNING,
                message=(
                    f"This resume presents {float(shown):g} years of experience to meet the JD. "
                    f"{held} The timeline was laid out across the same companies to cover it."
                ),
                content_path="experience",
                claim_text=f"{float(shown):g} years of experience",
                evidence={
                    "presented_years": float(shown),
                    "recorded_years": float(before) if before is not None else None,
                    "required_years": wanted,
                },
            )
        ]

    # Nothing was expanded — either the rule stood down because the operator
    # typed the dates, or the timeline already covered the JD. Only the first
    # case can leave the resume short, and the dates on the page decide it.
    if wanted and not shown:
        span = ground_rules.current_experience_years(content)
        if span is not None and span < float(wanted):
            return [
                Finding(
                    warning_type=ValidationWarningType.EXPERIENCE_BELOW_REQUIREMENT,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"The JD asks for {wanted:g}+ years; the employment dates on this resume "
                        f"span {span:g}. It is {wanted - span:g} years short of the stated minimum."
                    ),
                    content_path="experience",
                    claim_text=f"{span:g} years of experience",
                    evidence={"required_years": wanted, "resume_years": span},
                )
            ]
    return []


def _check_missing_mandatory_skills(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[Finding]:
    """Mandatory JD skills the resume cannot evidence.

    The composition deliberately does NOT add these — inventing a skill is the
    one thing the moulding step may never do. So the gap has to surface
    somewhere, and this is it: the operator learns what will be probed at
    interview while there is still time to pick a different profile.

    A fact, not an opinion: the JD lists a skill, the resume does not contain
    it, and both halves are checkable.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    if requirement is None:
        return []

    on_resume = {t.lower() for t in content.all_technologies()}
    missing = sorted(
        {
            tech.name
            for link, tech in (
                db.query(RequirementTechnology, Technology)
                .join(Technology, Technology.id == RequirementTechnology.technology_id)
                .filter(RequirementTechnology.requirement_id == requirement.id)
                .all()
            )
            if tech and link.importance == "MANDATORY" and tech.name.lower() not in on_resume
        }
    )
    if not missing:
        return []
    return [
        Finding(
            warning_type=ValidationWarningType.MANDATORY_SKILL_MISSING,
            severity=ValidationSeverity.WARNING,
            message=(
                f"The JD requires {', '.join(missing)}, and this profile does not evidence "
                f"{'them' if len(missing) > 1 else 'it'}. Nothing was added to the resume to "
                f"cover the gap — expect {'these' if len(missing) > 1 else 'this'} to be probed."
            ),
            content_path="skill_groups",
            claim_text=", ".join(missing),
            evidence={"missing_mandatory": missing},
        )
    ]


def _check_employer_without_projects(db: Session, content: ResumeContent) -> list[Finding]:
    """An employer the resume shows but has nothing to say about.

    Almost always the era rules doing their job on a thin bank: a JD asking
    eight years puts an employer in 2020, and if every project on record names
    something released in 2023 there is nothing that job could honestly have
    built. The composition will not fake it, so the employer prints with just
    a title and dates.

    That is not wrong, but it is worth knowing: the operator can add an
    older project to the bank, or ask for fewer companies so the timeline does
    not reach back so far. Saying nothing leaves them staring at a bare heading
    wondering what broke.
    """
    if not content.projects:
        return []
    with_projects = {p.source_company_id for p in content.projects if p.source_company_id}
    findings: list[Finding] = []
    for index, exp in enumerate(content.experience):
        if not exp.source_company_id or exp.source_company_id in with_projects:
            continue
        findings.append(
            Finding(
                warning_type=ValidationWarningType.EMPLOYER_WITHOUT_PROJECTS,
                severity=ValidationSeverity.INFO,
                message=(
                    f"{exp.company} ({exp.start_label}–{exp.end_label}) shows no projects: nothing "
                    f"in the bank is old enough to have been built in that period. Add an "
                    f"era-appropriate project, or generate with fewer companies so the timeline "
                    f"does not reach back this far."
                ),
                content_path=f"experience[{index}]",
                claim_text=exp.company,
                evidence={"company": exp.company, "period": f"{exp.start_label}-{exp.end_label}"},
            )
        )
    return findings


def _check_timeline_anachronisms(db: Session, content: ResumeContent) -> list[Finding]:
    """Something named in a period before it existed.

    The strongest factual check on the document, and the one a reader is most
    likely to make themselves: each employment states the years it ran, each
    technology states when it shipped, and one of those numbers cannot be
    bigger than the other. A project inherits the period of the employer it
    sits under — that pairing is what dates it (see
    ``resume_composer._pair_projects_to_companies``).

    ``app/services/era.py`` holds the dates and does the scanning, so this rule
    and the constraints written into the composer prompt cannot disagree about
    when anything was released.
    """
    releases = era.technology_release_years(db)
    if not releases:
        return []

    year_by_company: dict[str, int] = {}
    findings: list[Finding] = []

    for index, exp in enumerate(content.experience):
        if era.start_year_of(exp.start_label) is None:
            continue
        # Content is judged against the year the job ENDED — work done in the
        # last month of a job is still work done at that job — while the title
        # is judged against the year it started, because one title is printed
        # for the whole run. See era.end_year_of.
        year = era.end_year_of(exp.end_label)
        if exp.source_company_id:
            year_by_company[exp.source_company_id] = year
        texts = [(f"experience[{index}].designation", exp.designation or "")]
        texts += [(f"experience[{index}].highlights[{j}]", h) for j, h in enumerate(exp.highlights)]
        for path, text in texts:
            bound = era.start_year_of(exp.start_label) if path.endswith("designation") else year
            for hit in era.anachronisms_in(text, bound, releases):
                findings.append(
                    Finding(
                        warning_type=ValidationWarningType.TIMELINE_ANACHRONISM,
                        severity=ValidationSeverity.CRITICAL,
                        message=(
                            f"'{hit.term}' is claimed at {exp.company}, whose period ends {bound}, "
                            f"but {hit.term} was not released until {hit.released}. Reword this to "
                            f"what the work actually used at the time."
                        ),
                        content_path=path,
                        claim_text=text[:500],
                        evidence={"term": hit.term, "released": hit.released, "period_bound": bound},
                    )
                )

    for index, project in enumerate(content.projects):
        year = year_by_company.get(project.source_company_id or "")
        if year is None:
            continue
        texts = [
            (f"projects[{index}].name", project.name),
            (f"projects[{index}].description", project.description),
        ]
        texts += [(f"projects[{index}].technologies", t) for t in project.technologies]
        texts += [
            (f"projects[{index}].responsibilities[{j}]", r)
            for j, r in enumerate(project.responsibilities)
        ]
        texts += [
            (f"projects[{index}].achievements[{j}]", a) for j, a in enumerate(project.achievements)
        ]
        for path, text in texts:
            for hit in era.anachronisms_in(text, year, releases):
                findings.append(
                    Finding(
                        warning_type=ValidationWarningType.TIMELINE_ANACHRONISM,
                        severity=ValidationSeverity.CRITICAL,
                        message=(
                            f"'{project.name}' sits under {project.company or 'an employer'}, whose "
                            f"period ends {year}, but names {hit.term}, which was not released until "
                            f"{hit.released}. Either reword it or move the project to a later role."
                        ),
                        content_path=path,
                        claim_text=text[:500],
                        evidence={
                            "term": hit.term,
                            "released": hit.released,
                            "period_bound": year,
                            "project": project.name,
                        },
                    )
                )
    return findings


def _check_role_progression(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[Finding]:
    """A job title held before that job existed.

    The same class of error as an anachronistic technology, and the one the
    moulding step is most tempted into: every employment reads better carrying
    the JD's own title, so a 2018 role becomes "AI Engineer" three years before
    anyone had that title. The career has to progress toward the target role,
    not start at it.
    """
    findings: list[Finding] = []
    for index, exp in enumerate(content.experience):
        year = era.start_year_of(exp.start_label)
        if year is None or not (exp.designation or "").strip():
            continue
        # Dated per printed designation rather than from a table of titles.
        # Resolution is cached on the role, and designations repeat heavily
        # across resumes, so this is a handful of calls for the platform's
        # whole life rather than one per check.
        timeline = role_timeline.resolve(db, exp.designation)
        emerged = timeline.emerged
        if not timeline.resolved or emerged is None or emerged <= year:
            continue
        honest = timeline.title_for(year)
        findings.append(
            Finding(
                warning_type=ValidationWarningType.ROLE_ANACHRONISM,
                severity=ValidationSeverity.CRITICAL,
                message=(
                    f"'{exp.designation}' is dated from {year} at {exp.company}, but that title "
                    f"was not in use until around {emerged}. "
                    + (
                        f"'{honest}' is the honest equivalent for that period."
                        if honest and honest != exp.designation
                        else ""
                    )
                ),
                content_path=f"experience[{index}].designation",
                claim_text=exp.designation or "",
                evidence={
                    "designation": exp.designation,
                    "period_start": year,
                    "title_emerged": emerged,
                    "suggested": honest,
                },
            )
        )
    return findings


def _check_mandatory_skills_not_in_database(
    db: Session, session: ResumeCompositionSession
) -> list[Finding]:
    """A JD-mandatory skill that matches no technology row at all.

    Distinct from ``_check_missing_mandatory_skills``: that rule catches a
    skill the bank knows but this particular resume did not evidence — a
    composition choice. This one catches a skill the technology master has
    never heard of, which no composition, however chosen, can ever close.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    if requirement is None:
        return []
    labels = sorted({str(s).strip() for s in requirement.unmatched_mandatory_skills or [] if str(s).strip()})
    if not labels:
        return []
    return [
        Finding(
            warning_type=ValidationWarningType.MANDATORY_SKILL_NOT_IN_DATABASE,
            severity=ValidationSeverity.WARNING,
            message=(
                f"The JD requires {', '.join(labels)}, which "
                f"{'do' if len(labels) > 1 else 'does'} not match any technology on record at all. "
                f"No resume drawn from this bank can evidence {'these' if len(labels) > 1 else 'this'} "
                "until the technology master gains an entry for it."
            ),
            content_path="skill_groups",
            claim_text=", ".join(labels),
            evidence={"unmatched_mandatory": labels},
        )
    ]


def _check_no_direct_project_match(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[Finding]:
    """Nothing in the project bank shares the JD's role at all.

    ``autoselect_for_session`` still picks the closest-scoring projects
    available and moulds them toward the JD — that is the right behaviour for
    a thin bank — but a reader deciding whether to submit this resume needs to
    know the projects on it were the best available, not a direct match.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = requirement.parsed_role if requirement else None
    if not jd_role or not content.projects:
        return []
    best = max((jd_fit.role_similarity(jd_role, p.role) for p in content.projects), default=0)
    if best > 0:
        return []
    return [
        Finding(
            warning_type=ValidationWarningType.NO_DIRECT_PROJECT_MATCH,
            severity=ValidationSeverity.WARNING,
            message=(
                f"There are no projects available which directly resemble the role and "
                f"projects required for '{jd_role}'. The closest related projects in the "
                "bank were selected and moulded toward this JD instead."
            ),
            content_path="projects",
            evidence={"jd_role": jd_role},
        )
    ]


def _check_no_direct_company_match(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[Finding]:
    """Nothing in the company bank shares the JD's role at all — same shape as
    ``_check_no_direct_project_match``, for the experience section."""
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = requirement.parsed_role if requirement else None
    company_ids = [e.source_company_id for e in content.experience if e.source_company_id]
    if not jd_role or not company_ids:
        return []
    roles = {str(c.id): c.role for c in db.query(Company).filter(Company.id.in_(company_ids)).all()}
    best = max((jd_fit.role_similarity(jd_role, roles.get(cid)) for cid in company_ids), default=0)
    if best > 0:
        return []
    return [
        Finding(
            warning_type=ValidationWarningType.NO_DIRECT_COMPANY_MATCH,
            severity=ValidationSeverity.WARNING,
            message=(
                f"There are no employers in the bank whose role directly resembles "
                f"'{jd_role}'. The closest related companies were selected instead."
            ),
            content_path="experience",
            evidence={"jd_role": jd_role},
        )
    ]


def _check_technologies_against_profile(db: Session, session: ResumeCompositionSession, content: ResumeContent) -> list[Finding]:
    """Skills on the resume that the person's stored record does not support."""
    known = {
        tech.name.lower()
        for _, tech in db.query(PersonTechnology, Technology)
        .join(Technology, Technology.id == PersonTechnology.technology_id)
        .filter(PersonTechnology.person_id == session.person_id)
        .all()
    }
    if not known:
        return []

    findings: list[Finding] = []
    for group_index, group in enumerate(content.skill_groups):
        for item in group.items:
            if item.lower() not in known:
                findings.append(
                    Finding(
                        warning_type=ValidationWarningType.TECHNOLOGY_NOT_IN_PROFILE,
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"'{item}' appears in the skills section but is not recorded against this "
                            f"person. Either add it to their profile or remove it from the resume."
                        ),
                        content_path=f"skill_groups[{group_index}].items",
                        claim_text=item,
                        evidence={"technology": item},
                    )
                )
    return findings


#: What the judge is allowed to call a finding, and where it lands in the
#: warning taxonomy. Anything else in the reply is dropped rather than guessed
#: at — a judge that invents its own category is exactly the failure mode this
#: whole pass exists to catch, so it does not get the benefit of the doubt.
_GROUNDEDNESS_TYPES = {
    "MISDIRECTED_REFRAME": ValidationWarningType.MISDIRECTED_REFRAME,
    "UNGROUNDED_CLAIM": ValidationWarningType.UNGROUNDED_CLAIM,
    "OVERCLAIMED_SCOPE": ValidationWarningType.OVERCLAIMED_SCOPE,
}


def _groundedness_pairs(source: ResumeContent, content: ResumeContent) -> str:
    """Original-vs-moulded, side by side, for the judge to compare.

    Matched by ``source_project_id`` / ``source_company_id``, not by list
    position: ``_order_for_jd`` re-sorts ``content.projects`` by JD weight
    right after moulding, so by the time this runs the moulded list is no
    longer in the same order as the untouched ``source`` snapshot. A project
    with no ``source_project_id`` was composed in the studio, not pulled from
    the bank, so there is no original text to compare it against.
    """
    by_project_id = {p.source_project_id: p for p in source.projects if p.source_project_id}
    by_company_id = {e.source_company_id: e for e in source.experience if e.source_company_id}

    lines: list[str] = []
    for after in content.projects:
        before = by_project_id.get(after.source_project_id) if after.source_project_id else None
        if before is None:
            continue
        lines.append(
            f"PROJECT {after.name!r}\n"
            f"  ORIGINAL name: {before.name!r}\n"
            f"  ORIGINAL description: {before.description[:400]!r}\n"
            f"  MOULDED name: {after.name!r}\n"
            f"  MOULDED description: {after.description[:400]!r}\n"
            f"  MOULDED bullets: {after.responsibilities + after.achievements}"
        )
    for after in content.experience:
        before = by_company_id.get(after.source_company_id) if after.source_company_id else None
        if before is None or (before.designation or "") == (after.designation or ""):
            continue
        lines.append(
            f"EXPERIENCE at {after.company}\n"
            f"  ORIGINAL designation: {before.designation!r}\n"
            f"  MOULDED designation: {after.designation!r}"
        )
    if content.summary:
        lines.append(f"SUMMARY (newly written, no original to compare)\n  {content.summary!r}")
    return "\n\n".join(lines) or "(nothing was reworded)"


def _llm_groundedness_review(content: ResumeContent, source_content: ResumeContent) -> list[Finding]:
    """The second opinion: does the moulded copy still say what the original said?

    Deterministic rules can catch a technology used before it existed; they
    cannot tell "Research Pipeline" reworded honestly from "Research Pipeline"
    reworded into a different system, because that is a judgment about
    meaning, not arithmetic on two dates. This is the one check in the file
    that needs a reader rather than a comparison — so it gets one, with the
    original text supplied alongside the moulded text rather than the moulded
    text alone, which is what makes it a groundedness check rather than a
    vibe check.

    Optional and non-fatal: a broken or unreachable provider means this pass
    is silently skipped, never that generation fails.
    """
    provider = get_llm_provider()
    if provider.provider_name == "offline":
        return []

    pairs = _groundedness_pairs(source_content, content)
    if pairs == "(nothing was reworded)":
        return []

    try:
        response = provider.chat(
            [{"role": "user", "content": f"ORIGINAL vs MOULDED, project by project:\n\n{pairs}"}],
            model=LLM_MODEL,
            system=VALIDATION_SYSTEM,
            temperature=0.0,
            # A reasoning model spends part of this budget thinking before it
            # ever writes the JSON answer — 2000 left it truncated mid-thought
            # often enough that this pass was silently skipped almost every
            # time. The floor matches every other call in this app.
            # A judgement that runs out of room comes back shorter, not
            # failed: nobody sees a truncation, only a worse verdict.
            max_tokens=max(LLM_MAX_TOKENS, 16000),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        payload = json.loads(re.sub(r"^```(?:json)?|```$", "", response.text, flags=re.MULTILINE).strip())
    except (LLMTransientError, LLMPermanentError, json.JSONDecodeError, AttributeError) as exc:
        logger.info("Groundedness review skipped: %s", exc)
        return []

    findings: list[Finding] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or not item.get("issue"):
                continue
            warning_type = _GROUNDEDNESS_TYPES.get(str(item.get("type", "")).upper())
            if warning_type is None:
                continue
            severity = str(item.get("severity", "WARNING")).upper()
            findings.append(
                Finding(
                    warning_type=warning_type,
                    severity=severity if severity in ("INFO", "WARNING", "CRITICAL") else "WARNING",
                    message=str(item["issue"]),
                    content_path=str(item.get("location", "")) or None,
                    claim_text=str(item.get("claim", ""))[:500],
                    evidence={
                        "source": "groundedness_judge",
                        "original": str(item.get("original", ""))[:500],
                    },
                )
            )
    return findings


def _full_text_dump(content: ResumeContent) -> str:
    """Every reader-facing sentence on the resume, in one block for the judge."""
    lines = [f"HEADLINE: {content.contact.headline}", f"SUMMARY: {content.summary}"]
    for group in content.skill_groups:
        lines.append(f"SKILLS ({group.label}): {', '.join(group.items)}")
    for exp in content.experience:
        lines.append(
            f"EXPERIENCE at {exp.company} — {exp.designation or 'n/a'} "
            f"({exp.start_label or '?'}–{exp.end_label or '?'})"
        )
    for project in content.projects:
        lines.append(
            f"PROJECT {project.name!r} (role: {project.role or 'n/a'}): {project.description}\n"
            f"  bullets: {project.responsibilities + project.achievements}"
        )
    return "\n".join(lines)


def _llm_language_review(content: ResumeContent) -> list[Finding]:
    """The approval-time pass: language, grammar, spelling and internal
    consistency on the final, post-edit resume — whatever route it got there
    by (moulding, a manual edit, or the chat agent).

    Runs once, at approval, rather than after every edit: it is the same cost
    as the groundedness judge and the resume is only really "final" once the
    user has stopped changing it and clicked Approve.
    """
    provider = get_llm_provider()
    if provider.provider_name == "offline":
        return []

    dump = _full_text_dump(content)
    if not dump.strip():
        return []

    try:
        response = provider.chat(
            [{"role": "user", "content": f"FINAL RESUME TEXT:\n\n{dump}"}],
            model=LLM_MODEL,
            system=FINAL_REVIEW_SYSTEM,
            temperature=0.0,
            # See the groundedness call above — same reasoning-model headroom
            # issue, worse here since the whole resume text is in the prompt.
            # A judgement that runs out of room comes back shorter, not
            # failed: nobody sees a truncation, only a worse verdict.
            max_tokens=max(LLM_MAX_TOKENS, 16000),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        payload = json.loads(re.sub(r"^```(?:json)?|```$", "", response.text, flags=re.MULTILINE).strip())
    except (LLMTransientError, LLMPermanentError, json.JSONDecodeError, AttributeError) as exc:
        logger.info("Language review skipped: %s", exc)
        return []

    findings: list[Finding] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or not item.get("issue"):
                continue
            severity = str(item.get("severity", "WARNING")).upper()
            findings.append(
                Finding(
                    warning_type=ValidationWarningType.LANGUAGE_QUALITY_ISSUE,
                    severity=severity if severity in ("INFO", "WARNING", "CRITICAL") else "WARNING",
                    message=str(item["issue"]),
                    content_path=str(item.get("location", "")) or None,
                    claim_text=str(item.get("text", ""))[:500],
                    evidence={"source": "language_judge"},
                )
            )
    return findings


# -------------------------------------------------------------------- runner
def run_validation(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    *,
    unsupported_additions: list[dict] | None = None,
    source_content: ResumeContent | None = None,
    include_llm_pass: bool = False,
    include_language_pass: bool = False,
) -> list[Finding]:
    """Run every rule and return the findings. Never raises on rule failure.

    ``source_content`` is the pool content as it stood before moulding — pass
    it (generation always can; a manual edit has none) to enable the
    groundedness judge. Both this and the self-reported ``unsupported_additions``
    are checkable facts, not opinions about a deliberate composition choice, so
    neither is gated behind ``VALIDATION_STRICT_FACTS_ONLY``.

    ``include_language_pass`` runs the grammar/spelling/consistency judge —
    the approval-time pass (see ``resume_session_service.approve``), not run
    on every intermediate edit.
    """
    if not VALIDATION_ENABLED:
        return []

    findings: list[Finding] = []

    # The factual rules. Each compares two written-down things.
    #
    # The era/date rules that used to live here — anachronistic technology
    # against a release date, a job title against when it existed — are not
    # run by default any more. Composition is a manual step: the person
    # building the resume enters the dates, picks the technologies and owns
    # that data, the same way they own every other word on the page. A
    # platform-side "is this date right" pass on top of that was redundant
    # with the person doing the work, and it was also the slow half of
    # validation. They stay available — see the wider set below and
    # ``_check_timeline_anachronisms`` / ``_check_role_progression`` —
    # for anyone who wants them back.
    rules = [
        lambda: _check_unknown_technologies(db, content),
        lambda: _check_experience_claim(db, session, content),
        # The composition never invents a skill to close a JD gap, so the gap is
        # reported instead. Two written-down things compared, like the rest.
        lambda: _check_missing_mandatory_skills(db, session, content),
        lambda: _check_mandatory_skills_not_in_database(db, session),
        lambda: _check_no_direct_project_match(db, session, content),
        lambda: _check_no_direct_company_match(db, session, content),
        lambda: _check_employer_without_projects(db, content),
    ]

    if not VALIDATION_STRICT_FACTS_ONLY:
        # The wider, more opinionated set. Off by default: these flag deliberate
        # composition decisions as though they were mistakes.
        rules += [
            lambda: _check_anachronisms(content),
            lambda: _check_employment_timeline(db, session),
            lambda: _check_technologies_against_profile(db, session, content),
        ]

    for rule in rules:
        try:
            findings.extend(rule())
        except Exception:  # noqa: BLE001 — a broken rule must not break generation
            logger.exception("Validation rule failed")

    # Self-reported by the composer, not judged after the fact: the model said
    # outright it could not ground this, so there is nothing to weigh. Always on.
    for addition in unsupported_additions or []:
        findings.append(
            Finding(
                warning_type=ValidationWarningType.UNGROUNDED_CLAIM,
                severity=ValidationSeverity.WARNING,
                message=(
                    f"The assistant proposed a claim it could not ground in the stored profile: "
                    f"{addition.get('reason', 'no reason given')}"
                ),
                claim_text=str(addition.get("claim", ""))[:500],
                evidence={"source": "composer"},
            )
        )

    # The groundedness judge: original vs moulded, project by project. Needs
    # something to compare against, so it only runs when a source snapshot was
    # actually taken (moulding ran) and the caller asked for it.
    if include_llm_pass and source_content is not None:
        try:
            findings.extend(_llm_groundedness_review(content, source_content))
        except Exception:  # noqa: BLE001 — a broken judge must not break generation
            logger.exception("Groundedness review failed")

    # The approval-time language/grammar/spelling/consistency pass — see
    # ``resume_session_service.approve``. Independent of the groundedness
    # judge: it needs no "before" text, only the final copy.
    if include_language_pass:
        try:
            findings.extend(_llm_language_review(content))
        except Exception:  # noqa: BLE001 — a broken judge must not break approval
            logger.exception("Language review failed")

    return findings
