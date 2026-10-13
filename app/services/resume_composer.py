"""Section 19 — base resume generation.

Deterministic assembly from the pool first, then an LLM pass for the narrative
parts (summary, project descriptions) only. Structure — which projects, which
companies, which technologies, in what order — is decided by data and by the
user's selections, never by free-text generation. That is what keeps the studio
edits in section 22 reliable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import (
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    RESUME_MAX_BULLETS_PER_PROJECT,
    RESUME_MAX_COMPANIES,
    RESUME_MAX_DESCRIPTION_CHARS,
    RESUME_MAX_PROJECTS,
)
from app.models.company import Company
from app.models.person import Person
from app.models.project import Project, ProjectTechnology
from app.models.requirement import Requirement, RequirementTechnology
from app.models.resume_session import (
    ResumeCompositionSession,
    ResumeSelectedCompany,
    ResumeSelectedProject,
    ResumeSelectedTechnology,
)
from app.models.resume_version import ResumeVersion
from app.models.technology import Technology
from app.prompts.templates import (
    EMPLOYER_SYSTEM,
    EMPLOYER_USER,
    PROJECT_REFRAME_SYSTEM,
    PROJECT_SYSTEM,
    PROJECT_USER,
)
from app.schemas.resume_content import (
    ContactBlock,
    ExperienceBlock,
    ProjectBlock,
    ResumeContent,
    SkillGroup,
)
from app.services import era, ground_rules, jd_fit, progress, summary_templates, summary_writer
from app.services import role_timeline as role_timeline_service
from app.services.llm.base import LLMPermanentError, LLMTransientError, extract_json
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry
from app.utils.enums import ResumeVersionStatus, SkillImportance
from app.utils.exceptions import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)

#: The moulding reply is a whole JSON document — every project re-described and
#: every designation realigned, for two full pages — on top of whatever a
#: reasoning model spends drafting inside <think> before it writes any of it.
#: The largest single answer this platform asks for, and the one where running
#: short shows up directly as thin bullets rather than as an error.
MOULD_TOKENS = 48000
MOULD_ATTEMPTS = 3

#: Token ceiling for ONE unit of the fan-out. The one-shot call needed 48k
#: because it drafted the whole resume inside its scratchpad; a single
#: project or employer measured under 2k of completion tokens, so this is
#: generous rather than tight, and a unit that runs away fails fast into a
#: retry instead of grinding for a quarter of an hour.
UNIT_TOKENS = 8000

#: How many units are in flight at once. The shipped shape tops out at five
#: projects and five employers, so twelve clears the whole resume in ONE wave.
#: This is the number that decides the wall clock: at six, a ten-unit resume
#: ran in two waves and took 136s -- twice the slowest unit -- for no reason
#: other than the pool size. The fan-out is latency-bound, not CPU-bound; the
#: threads are asleep on a socket, so the cost of more of them is only the
#: provider's concurrency limit.
MOULD_MAX_PARALLEL = 12


# --------------------------------------------------------------- auto-selection
#: The resume is one page, so the shortlist IS the resume: four or five projects
#: that answer the JD and four or five employers whose work answers the role.
#: Both counts are the one-page budget (see app/config), not a starting point to
#: be trimmed later — proposing six and cutting one at render time would mean
#: the operator judged a project that was never going to print.
AUTOSELECT_PROJECTS = RESUME_MAX_PROJECTS
AUTOSELECT_COMPANIES = RESUME_MAX_COMPANIES


def jd_project_score(
    project: Project,
    *,
    jd_role: str | None,
    domain: str | None,
    jd_tokens: set[str],
    role_counts: dict[str, int],
    pool_size: int,
    tech_sim: int = 0,
) -> int:
    """How well one bank project answers this JD, 0-100.

    The single definition, shared by generation and by "add one more project"
    from the chat. They ranked separately before and had already drifted: one
    was domain-led and rarity-aware, the other still weighted a role label
    three quarters of the bank shares at 0.65. Two answers to one question is
    the bug; which formula wins is a detail.

    See the weighting note in ``autoselect_for_session`` for why domain leads.
    """
    role_sim = jd_fit.role_similarity(jd_role, project.role)
    rarity = jd_fit.label_discrimination(role_counts, project.role, pool_size)
    domain_sim = jd_fit.domain_affinity(
        domain,
        jd_tokens,
        project.short_description,
        project.detailed_description,
        " ".join(project.responsibilities or []),
    )
    terms = [(0.15 * rarity, role_sim), (0.60, domain_sim), (0.25, tech_sim)]
    weight = sum(w for w, _ in terms)
    if weight <= 0:
        return 0
    return round(sum(w * v for w, v in terms) / weight)


def used_projects_and_companies_for_requirement(
    db: Session, requirement_id: UUID, *, exclude_session_id: UUID | None = None
) -> tuple[set[UUID], set[UUID]]:
    """Projects/companies already spent by an approved resume under this JD.

    A second resume raised against the same JD is a different candidate
    telling a different story to the same vendor, not the same story retold —
    so once a resume has been approved, the projects and companies it used are
    taken off the table for every OTHER resume against this JD. Read from the
    frozen APPROVED version's content rather than the live selection rows: a
    session that gets reopened and re-edited after approval must not be able
    to quietly change what an already-delivered resume is remembered as having
    used.

    ``exclude_session_id`` leaves the caller's own session out of its own
    exclusion set, so a session is never blocked from re-selecting what it
    itself already used.
    """
    sessions = db.query(ResumeCompositionSession.id).filter(
        ResumeCompositionSession.requirement_id == requirement_id
    )
    if exclude_session_id is not None:
        sessions = sessions.filter(ResumeCompositionSession.id != exclude_session_id)
    session_ids = [row[0] for row in sessions.all()]
    if not session_ids:
        return set(), set()

    used_projects: set[UUID] = set()
    used_companies: set[UUID] = set()
    approved_versions = db.query(ResumeVersion).filter(
        ResumeVersion.session_id.in_(session_ids),
        ResumeVersion.status == ResumeVersionStatus.APPROVED,
    )
    for version in approved_versions:
        content = version.content_json or {}
        for project in content.get("projects") or []:
            pid = project.get("source_project_id")
            if pid:
                used_projects.add(UUID(pid))
        for exp in content.get("experience") or []:
            cid = exp.get("source_company_id")
            if cid:
                used_companies.add(UUID(cid))
    return used_projects, used_companies


def autoselect_for_session(
    db: Session,
    session: ResumeCompositionSession,
    *,
    company_count: int | None = None,
    project_count: int | None = None,
) -> None:
    """Propose the best-fitting set of assets in the bank for this JD.

    The bank is a shared pool, not a per-person filing cabinet. A person record
    carries only who they are — name, phone, email — and every project, company
    and technology in the database is available to any resume. So the question
    at generation time is not "what has this person done", it is "what does this
    JD ask for, and what does the bank hold that answers it".

    Projects and companies are independent pools, each matched to the JD's own
    role by name — neither carries a link to the other, or to any person.
    Ranking combines a role-similarity search (``jd_fit.role_similarity``,
    already used to compare job titles elsewhere) with each row's own
    description text weighed topically against the JD (``jd_fit.topical_overlap``)
    — role dominant, description secondary — with an unrelated tie-breaker for
    projects only: technology overlap with what the JD asks for. Technologies
    follow the same idea: named outright by the JD, or related to it by
    category/description, is what gets on the resume at all — see the
    technology block below.

    ``company_count`` and ``project_count`` come from the generation form. They
    are what the operator asked for, so they override the house defaults —
    bounded by what the bank can actually field, since three companies on
    record cannot be made into five.

    Everything proposed here is a starting point. The operator reshapes it in
    the Composition tab, and the master pool is never modified either way.
    """
    want_projects = project_count or AUTOSELECT_PROJECTS
    want_companies = company_count or AUTOSELECT_COMPANIES
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = requirement.parsed_role if requirement else None
    # The JD's own vocabulary — role, responsibilities, domain — for judging a
    # bank row's description/category against the JD topically, not just by
    # title. Shared across all three pools below.
    jd_tokens = jd_fit.jd_context_tokens(requirement)
    # A second resume against the same JD must tell a different story: the
    # projects and companies an earlier APPROVED resume for this requirement
    # already used are off the table here, never just deprioritised.
    used_project_ids, used_company_ids = used_projects_and_companies_for_requirement(
        db, session.requirement_id, exclude_session_id=session.id
    )
    jd_tech_ids: set[UUID] = set()
    mandatory_ids: set[UUID] = set()
    if requirement:
        for row in db.query(RequirementTechnology).filter(
            RequirementTechnology.requirement_id == requirement.id
        ):
            if row.technology_id:
                jd_tech_ids.add(row.technology_id)
                if row.importance == SkillImportance.MANDATORY:
                    mandatory_ids.add(row.technology_id)

    # --- projects: role match first, short description second -----------------
    if not session.selected_projects:
        projects = (
            db.query(Project)
            .filter(Project.is_active.is_(True), Project.id.notin_(used_project_ids))
            .all()
        )
        if not projects:
            raise ConflictError(
                "Every project in the bank has already been used in an approved resume for "
                "this JD. Add more projects to the bank before generating another resume against it."
            )
        tech_by_project: dict[UUID, set[UUID]] = {}
        for link in db.query(ProjectTechnology).filter(
            ProjectTechnology.project_id.in_([p.id for p in projects] or [None])
        ):
            tech_by_project.setdefault(link.project_id, set()).add(link.technology_id)

        def jd_weight(project: Project) -> int:
            stack = tech_by_project.get(project.id, set())
            # A mandatory skill is worth more than a merely preferred one, so a
            # project evidencing what the JD insists on outranks a broader one.
            return len(stack & mandatory_ids) * 2 + len(stack & jd_tech_ids)

        # The most this JD could ask of any one project, so the overlap below
        # can be expressed on the same 0-100 scale as the other two signals.
        jd_weight_max = len(mandatory_ids) * 2 + len(jd_tech_ids)

        # How much each role label discriminates within THIS pool, so a label
        # 73% of the bank shares stops speaking louder than the evidence.
        project_role_counts = jd_fit.label_counts([p.role for p in projects])

        def project_relevance(project: Project) -> int:
            # Scored by jd_project_score, which the chat's "add one more
            # project" also calls, so the two can never disagree about which
            # project answers this JD best. The technology overlap is computed
            # here because only generation knows the JD's technology set.
            tech_sim = round(100 * jd_weight(project) / jd_weight_max) if jd_weight_max else 0
            return jd_project_score(
                project,
                jd_role=jd_role,
                domain=requirement.domain if requirement else None,
                jd_tokens=jd_tokens,
                role_counts=project_role_counts,
                pool_size=len(projects),
                tech_sim=tech_sim,
            )

        def score(project: Project) -> tuple[int, int, str]:
            return (-project_relevance(project), -jd_weight(project), project.name.lower())

        # Four or five, never more: the page holds that many and the operator
        # can judge every one of them in the Composition tab.
        chosen = sorted(projects, key=score)[:want_projects]
        for order, project in enumerate(chosen):
            db.add(
                ResumeSelectedProject(
                    session_id=session.id,
                    project_id=project.id,
                    display_order=order,
                    is_primary="true" if order == 0 else None,
                )
            )

    # --- companies: ranked on role match ---------------------------------------
    if not session.selected_companies:
        companies = (
            db.query(Company)
            .filter(Company.is_active.is_(True), Company.id.notin_(used_company_ids))
            .order_by(Company.name)
            .all()
        )
        if not companies:
            raise ConflictError(
                "Every company in the bank has already been used in an approved resume for "
                "this JD. Add more companies to the bank before generating another resume against it."
            )

        # Companies carry even less that discriminates than projects do: the
        # only signal was the role label, and in the live bank ten of the
        # twenty-one are filed as "Full Stack Developer". Ten identical scores
        # meant the choice of employer fell through to the alphabetical
        # tie-break below — Acme before Zenith, with the JD having no say.
        company_role_counts = jd_fit.label_counts([c.role for c in companies])

        def company_relevance(company: Company) -> int:
            role_sim = jd_fit.role_similarity(jd_role, company.role)
            rarity = jd_fit.label_discrimination(
                company_role_counts, company.role, len(companies)
            )
            # There is no description column here, so the name and whatever
            # was dropped in metadata are the only prose to read. Thin, but a
            # company called "FinLync Payments" against a FinTech JD is
            # exactly the match the role label cannot make.
            domain_sim = jd_fit.domain_affinity(
                requirement.domain if requirement else None,
                jd_tokens,
                company.name,
                company.metadata_json,
            )
            terms = [(0.65 * rarity, role_sim), (0.35, domain_sim)]
            weight = sum(w for w, _ in terms)
            if weight <= 0:
                return 0
            return round(sum(w * v for w, v in terms) / weight)

        scored = sorted(companies, key=lambda c: (-company_relevance(c), c.name.lower()))
        chosen_companies = [c.id for c in scored if company_relevance(c) > 0][:want_companies]

        # Only when the bank cannot field four employers whose role resembles
        # this one: an experience section thinner than that reads as an
        # incomplete career, which costs more than a weakly-matched row.
        floor = min(want_companies, len(companies))
        for company in scored:
            if len(chosen_companies) >= floor:
                break
            if company.id not in chosen_companies:
                chosen_companies.append(company.id)

        for order, company_id in enumerate(chosen_companies[:want_companies]):
            db.add(ResumeSelectedCompany(session_id=session.id, company_id=company_id, display_order=order))

    # --- technologies: what the JD asks for first, then the rest of the bank --
    if session.selected_technologies:
        # Which technologies appear is the operator's choice and is left alone.
        # Their tier is not a choice — importance is a property of the JD — so it
        # is refreshed here. Without this, a session created before the JD's
        # skills were resolved keeps every skill tiered SECONDARY forever.
        for selection in session.selected_technologies:
            selection.tier = "PRIMARY" if selection.technology_id in mandatory_ids else selection.tier
            selection.is_jd_driven = "true" if selection.technology_id in jd_tech_ids else None
        db.flush()

    if not session.selected_technologies:
        technologies = db.query(Technology).filter(Technology.is_active.is_(True)).all()

        def tech_topic(tech: Technology) -> int:
            text = " ".join(filter(None, [tech.category, tech.sub_category, tech.description]))
            return jd_fit.topical_overlap(jd_tokens, text)

        # Related to this JD: named outright (mandatory/preferred), or its
        # category/description speaks to the JD's own words. A resume printing
        # every technology on record regardless of the JD reads as noise, so
        # the rest of the bank sits out rather than being reordered to the end.
        relevant = [
            tech for tech in technologies
            if tech.id in mandatory_ids or tech.id in jd_tech_ids or tech_topic(tech) > 0
        ]
        # A JD too generic to match anything topically still needs a skills
        # section printed: fall back to the full bank rather than nothing.
        pool = relevant or technologies
        ordered = sorted(
            pool,
            key=lambda tech: (
                0 if tech.id in mandatory_ids else (1 if tech.id in jd_tech_ids else 2),
                -tech_topic(tech),
                (tech.category or "").lower(),
                tech.name.lower(),
            ),
        )
        for order, tech in enumerate(ordered):
            db.add(
                ResumeSelectedTechnology(
                    session_id=session.id,
                    technology_id=tech.id,
                    section_label=tech.category or "Technologies",
                    # Importance belongs to the JD, never to the master row.
                    tier="PRIMARY" if tech.id in mandatory_ids else "SECONDARY",
                    display_order=order,
                    is_jd_driven="true" if tech.id in jd_tech_ids else None,
                )
            )

    db.flush()


# -------------------------------------------------------------- pool assembly
def _project_ref(index: int, block: ProjectBlock) -> str:
    """A stable handle for one project across the round-trip to the model."""
    return block.source_project_id or f"composed-{index}"


def _project_blocks(db: Session, session: ResumeCompositionSession) -> list[ProjectBlock]:
    # The cap is applied to the SELECTION, before any block is built: a session
    # created before the limit existed, or one an operator loaded up through the
    # API, still yields a one-page resume rather than a two-page one.
    selections = (
        db.query(ResumeSelectedProject)
        .filter(ResumeSelectedProject.session_id == session.id)
        .order_by(ResumeSelectedProject.display_order)
        .limit(RESUME_MAX_PROJECTS)
        .all()
    )
    blocks: list[ProjectBlock] = []
    for selection in selections:
        project = (
            db.query(Project).filter(Project.id == selection.project_id).first()
            if selection.project_id
            else None
        )
        # A row with no bank project is one the operator composed in the studio.
        # It is described entirely by its overrides and is dropped only when it
        # has no title at all, which the payload validator already prevents.
        if project is None and selection.project_id is not None:
            continue
        if project is None and not (selection.title_override or "").strip():
            continue

        if selection.technologies_override:
            tech_names = list(selection.technologies_override)
        elif project is not None:
            tech_names = [
                tech.name
                for _, tech in db.query(ProjectTechnology, Technology)
                .join(Technology, Technology.id == ProjectTechnology.technology_id)
                .filter(ProjectTechnology.project_id == project.id)
                .all()
            ]
        else:
            tech_names = []

        # Which employer this project prints under. The pairing is decided once
        # (see pair_projects_to_companies) and held on the selection row, so a
        # rebuild does not reshuffle the resume's own history.
        paired = (
            db.query(Company).filter(Company.id == selection.company_id).first()
            if selection.company_id
            else None
        )

        blocks.append(
            ProjectBlock(
                source_project_id=str(project.id) if project is not None else None,
                company=paired.name if paired else None,
                source_company_id=str(paired.id) if paired else None,
                name=selection.title_override or (project.name if project else ""),
                role=selection.role_override or (project.role if project else None),
                # Version 1 prints the detailed description; short_description
                # (and role, above) are used only to select and rank this
                # project against the JD — see project_relevance above — never
                # what actually reaches the page.
                description=selection.description_override
                or (project.detailed_description if project else None)
                or (project.short_description if project else None)
                or "",
                responsibilities=list(
                    selection.responsibilities_override
                    or (project.responsibilities if project else None)
                    or []
                ),
                achievements=list(selection.achievements_override or []),
                technologies=tech_names,
            )
        )
    return blocks


def _experience_blocks(
    db: Session, session: ResumeCompositionSession, *, jd_role: str | None = None
) -> list[ExperienceBlock]:
    """Designation and dates come from the composed resume, never from a
    stored employment record — see ``persist_composed_overrides``, which
    writes what generation decided back onto these override columns. There is
    deliberately no fallback to a "real" record here: nothing about how long
    somebody actually held a title is meant to reach the page verbatim.

    A company added in the Composition tab with no designation of its own
    falls back to the JD's own role — the same honest default the headline
    uses — rather than printing blank. Dates are left for
    ``ground_rules.meet_jd_experience`` to fill in on the next rebuild; unlike
    a designation, a date range has to be weighed against every other company
    on the resume, which only that pass can do.
    """
    selections = (
        db.query(ResumeSelectedCompany)
        .filter(ResumeSelectedCompany.session_id == session.id)
        .order_by(ResumeSelectedCompany.display_order)
        .limit(RESUME_MAX_COMPANIES)
        .all()
    )
    blocks: list[ExperienceBlock] = []
    for selection in selections:
        company = db.query(Company).filter(Company.id == selection.company_id).first()
        if company is None:
            continue
        blocks.append(
            ExperienceBlock(
                source_company_id=str(company.id),
                company=company.name,
                designation=selection.designation_override or jd_role,
                start_label=selection.start_date_override,
                end_label=selection.end_date_override or "Present",
                # Both written by the composer pass, which is the only step
                # that has seen the JD. Left empty here rather than filled with
                # something stored: an employer's line on a resume is composed
                # for the role in front of it, exactly like a project's.
                description="",
                highlights=[],
            )
        )
    return blocks


def _skill_groups(db: Session, session: ResumeCompositionSession) -> list[SkillGroup]:
    selections = (
        db.query(ResumeSelectedTechnology, Technology)
        .join(Technology, Technology.id == ResumeSelectedTechnology.technology_id)
        .filter(ResumeSelectedTechnology.session_id == session.id)
        .order_by(ResumeSelectedTechnology.display_order)
        .all()
    )
    # One row per label. Grouping by (label, tier) printed "Cloud:" twice on the
    # same resume - once for the JD-mandatory entries and once for the rest -
    # which reads as a bug. The tier still decides order: within a row the
    # primaries lead, and rows carrying primaries lead the section.
    grouped: dict[str, dict[str, list[str]]] = {}
    for selection, tech in selections:
        label = selection.section_label or tech.category or "Technologies"
        tier = selection.tier if selection.tier in ("PRIMARY", "SECONDARY") else "PRIMARY"
        # Three buckets, not two. A JD's PREFERRED skills used to be tiered
        # SECONDARY, which put them in with every unrelated technology in the
        # bank -- and ground_rules.fit_one_page then cuts each row to
        # RESUME_MAX_SKILLS_PER_GROUP. With ~190 technologies selected and 14
        # printed per row, a skill the JD actually asked for was being dropped
        # in favour of one it never mentioned. Mandatory skills survived only
        # because they are PRIMARY. is_jd_driven already marks both kinds; it
        # just was not being used for ordering.
        if tier == "PRIMARY":
            bucket = "PRIMARY"
        elif selection.is_jd_driven:
            bucket = "JD_PREFERRED"
        else:
            bucket = "SECONDARY"
        grouped.setdefault(
            label, {"PRIMARY": [], "JD_PREFERRED": [], "SECONDARY": []}
        )[bucket].append(tech.name)

    groups: list[SkillGroup] = []
    for label, tiers in grouped.items():
        items = tiers["PRIMARY"] + tiers["JD_PREFERRED"] + tiers["SECONDARY"]
        if not items:
            continue
        groups.append(
            SkillGroup(
                label=label,
                tier="PRIMARY" if tiers["PRIMARY"] else "SECONDARY",
                items=items,
            )
        )
    groups.sort(
        # Rows carrying what the JD asked for lead the section, ordered by how
        # much of it they carry — mandatory counting for more than preferred.
        key=lambda g: (
            0 if g.tier == "PRIMARY" else 1,
            -(len(grouped[g.label]["PRIMARY"]) * 2 + len(grouped[g.label]["JD_PREFERRED"])),
            g.label.lower(),
        )
    )
    return groups


def build_pool_content(db: Session, session: ResumeCompositionSession) -> ResumeContent:
    """Assemble the deterministic skeleton from the bank + this session's selections.

    A person contributes only their identity — name, phone, email. There is no
    stored designation, no total experience and no base resume to start from:
    the headline is the JD's role, and the body is whatever the composition step
    selected out of the shared pool. That is what makes creating a person a
    three-field job.
    """
    person = db.query(Person).filter(Person.id == session.person_id).first()
    if person is None:
        raise NotFoundError("The person for this session no longer exists")

    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = (requirement.parsed_role if requirement else "") or None

    return ResumeContent(
        contact=ContactBlock(
            full_name=person.full_name,
            # The role being applied for, not a title anyone stored.
            headline=jd_role or "",
            email=person.email,
            phone=person.phone,
        ),
        summary="",
        skill_groups=_skill_groups(db, session),
        experience=_experience_blocks(db, session, jd_role=jd_role),
        projects=_project_blocks(db, session),
        # Education is dropped by the house rules on every resume anyway, and is
        # no longer stored on a person.
        education=[],
        certifications=[],
    )


# ------------------------------------------------------------------ LLM pass
def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


def _one_json_call(
    provider,
    *,
    system: str,
    user: str,
    label: str,
    max_tokens: int,
) -> dict:
    """One focused model call that must come back as a JSON object.

    Every unit of the moulding fan-out goes through here, so the retry rule,
    the fence stripping and the "empty reply" case are written once. Returns an
    empty dict rather than raising: one project that would not come back is a
    project left as the bank recorded it, which is a worse resume but still a
    correct one — losing the other five to it would not be.
    """

    def _call():
        return provider.chat(
            [{"role": "user", "content": user}],
            model=LLM_MODEL,
            system=system,
            temperature=LLM_TEMPERATURE,
            max_tokens=max_tokens,
            timeout=max(LLM_TIMEOUT_SECONDS, 300.0),
        )

    for attempt in range(MOULD_ATTEMPTS):
        unit_started = time.monotonic()
        try:
            response = with_retry(_call, max_retries=LLM_MAX_RETRIES)
        except (LLMTransientError, LLMPermanentError) as exc:
            logger.warning("%s failed (attempt %d): %s", label, attempt + 1, exc)
            continue
        # Per-unit timing is the only way to tell a slow model from a provider
        # serialising our requests: if every unit's own time is short but the
        # fan-out's wall clock is their sum, the concurrency is not real.
        logger.info("%s took %.1fs", label, time.monotonic() - unit_started)
        text = _strip_fences(response.text)
        if not text:
            logger.warning("%s came back empty (attempt %d/%d)", label, attempt + 1, MOULD_ATTEMPTS)
            continue
        try:
            parsed = json.loads(extract_json(text))
        except json.JSONDecodeError as exc:
            logger.warning(
                "%s was not JSON (attempt %d/%d): %s | head=%r",
                label, attempt + 1, MOULD_ATTEMPTS, exc, text[:200],
            )
            continue
        if isinstance(parsed, dict):
            return parsed
        logger.warning("%s returned %s, not an object", label, type(parsed).__name__)
    logger.error("%s produced nothing after %d attempts", label, MOULD_ATTEMPTS)
    return {}


def _period_facts(period) -> tuple[str, str]:
    """The window and the forbidden-terms line for one employer's period."""
    if period is None:
        return "unknown", "nothing specific"
    window = f"{period.start_year or '?'}-{period.end_label or 'Present'}"
    forbidden = ", ".join(period.forbidden) if period.forbidden else "nothing specific"
    return window, forbidden


def _assign_missing_skills(
    projects: list[ProjectBlock],
    missing: list[str],
    period_by_company: dict,
) -> dict[int, list[str]]:
    """Decide, before any model call, which project carries which JD skill.

    The one job the whole-resume call did that no independent unit can: look at
    every project at once and put each unevidenced skill where it belongs.
    Splitting the call lost that, and the eval caught the consequence — asked
    for Kubernetes and Terraform, every unit independently judged them a poor
    fit for its own system, declined, and the resume answered a JD it never
    mentioned two hard requirements of. Each unit was right on its own terms;
    nobody was responsible for the whole.

    So the caller decides instead, and hands each unit only its own skills as a
    requirement rather than the full list as a suggestion. That fixes both ends
    of it: a skill can no longer land nowhere, and it can no longer land in
    every project at once, which is the other failure — the same tool named in
    five systems reads as decoration and a reader spots it immediately.

    Two rules pick the home, in order:

    * **The period must allow it.** A project under an employer whose years
      predate the technology is not a candidate at all. This outranks fit: a
      skill placed before it existed discredits the whole document, and no
      amount of relevance is worth that.
    * **Then the closest system wins**, scored on how much the skill's own
      words already overlap that project's stack and description, with ties
      going to whichever project is carrying the least so far. That last part
      is what spreads them out instead of loading them all onto whichever
      project happens to sort first.

    A skill no period can host is deliberately dropped rather than forced: the
    honest outcome is that this resume cannot evidence it, which the validation
    engine already reports as MANDATORY_SKILL_MISSING.
    """
    assigned: dict[int, list[str]] = {i: [] for i in range(len(projects))}
    if not projects:
        return assigned

    for skill in missing:
        needle = skill.lower()
        tokens = {t for t in re.split(r"[^a-z0-9+#.]+", needle) if len(t) > 2}

        best: tuple[int, int, int] | None = None  # (-score, load, index)
        for index, project in enumerate(projects):
            period = period_by_company.get((project.company or "").strip().lower())
            if period is not None and any(
                needle == term.lower() for term in (period.forbidden or [])
            ):
                continue  # the years cannot hold it

            haystack = " ".join(
                [project.description or "", " ".join(project.technologies or [])]
            ).lower()
            score = sum(1 for t in tokens if t in haystack)
            candidate = (-score, len(assigned[index]), index)
            if best is None or candidate < best:
                best = candidate

        if best is not None:
            assigned[best[2]].append(skill)
    return assigned


def _mould_one_project(provider, project, period, ctx: dict, must_place: list[str]) -> dict:
    """Reframe a single project toward the JD, inside its own period."""
    window, forbidden = _period_facts(period)

    lines: list[str] = []
    if project.responsibilities:
        lines.append("work on record - the raw material to angle at this JD:")
        lines += [f"  - {line}" for line in project.responsibilities[:8]]
    if project.achievements:
        lines.append("outcomes on record - keep every number exactly:")
        lines += [f"  - {line}" for line in project.achievements[:8]]
    if not lines:
        lines.append("no bullets on record - write only what the description supports")

    user = PROJECT_USER.format(
        role=ctx["role"],
        responsibilities=ctx["responsibilities"],
        missing_skills=(
            ", ".join(must_place)
            if must_place
            else "none - this project carries no unevidenced skill"
        ),
        name=project.name,
        company=project.company or "no employer",
        window=window,
        forbidden=forbidden,
        technologies=", ".join(project.technologies) or "n/a",
        description=project.description or "not described",
        bullets_block="\n".join(lines),
    )
    system = PROJECT_SYSTEM.format(
        max_bullets=RESUME_MAX_BULLETS_PER_PROJECT,
        max_description_chars=RESUME_MAX_DESCRIPTION_CHARS,
    )
    return _one_json_call(
        provider,
        system=system,
        user=user,
        label=f"Project mould [{project.name}]",
        max_tokens=max(LLM_MAX_TOKENS, UNIT_TOKENS),
    )


def _mould_one_employer(provider, block, period, owned, ctx: dict) -> dict:
    """Write one employer's description and standing remit."""
    window, forbidden = _period_facts(period)
    if owned:
        projects_block = "\n".join(
            f"  - {p.name}: {p.description or 'no description on record'} "
            f"({', '.join(p.technologies) or 'stack not recorded'})"
            for p in owned
        )
    else:
        projects_block = "  (no project on this resume sits under this employer)"

    user = EMPLOYER_USER.format(
        role=ctx["role"],
        responsibilities=ctx["responsibilities"],
        company=block.company,
        designation=block.designation or ctx["role"],
        window=window,
        forbidden=forbidden,
        projects_block=projects_block,
    )
    return _one_json_call(
        provider,
        system=EMPLOYER_SYSTEM,
        user=user,
        label=f"Employer mould [{block.company}]",
        max_tokens=max(LLM_MAX_TOKENS, UNIT_TOKENS),
    )


def _llm_narrative(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    *,
    periods: list | None = None,
) -> dict:
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()

    mandatory: list[str] = []
    preferred: list[str] = []
    if requirement:
        for req_tech, tech in (
            db.query(RequirementTechnology, Technology)
            .join(Technology, Technology.id == RequirementTechnology.technology_id)
            .filter(RequirementTechnology.requirement_id == requirement.id)
            .all()
        ):
            (mandatory if req_tech.importance == SkillImportance.MANDATORY else preferred).append(tech.name)

    # Naming only the JD's wanted skills invites the model to weave the missing
    # ones into the summary — "deployment on AWS and Azure" for a candidate who
    # has never touched Azure. Splitting the list is what stops that: the ones
    # to lead with, and the ones that must not appear at all.
    # What the resume itself carries is the honest test now: the composition
    # already selected the JD-relevant subset out of the bank, so anything not
    # on the page is something no project or skill row could cover.
    evidenced = {name.lower() for name in content.all_technologies()}
    # Only the unevidenced ones are handed out. The skills the resume already
    # shows need no placing; naming them again just invites every unit to weave
    # the same word into its own paragraph.
    missing = [name for name in mandatory if name.lower() not in evidenced]

    provider = get_llm_provider()

    # ── the fan-out ────────────────────────────────────────────────────
    #
    # One request per project and one per employer, all issued at once,
    # instead of a single request carrying the whole resume. Two reasons, and
    # the second is the one that matters:
    #
    #   * each unit is a sixth the prompt, so a reasoning model deliberates
    #     over one system rather than the entire document;
    #   * the units are independent, so the wall clock is the slowest single
    #     call rather than the sum of all of them.
    #
    # Measured on MiniMax-M3 with three projects and three employers: 256s for
    # the one-shot call, 23s for the fan-out, with the scratchpad left fully
    # enabled so nothing is traded away on quality. (Turning the scratchpad
    # off IS faster still, and it silently dropped the employer descriptions,
    # renamed projects and ignored the designations the timeline fixes — which
    # is why this reduces the QUESTION rather than the thinking.)
    #
    # Nothing inside a worker touches the database. Every row these need was
    # read above, on this thread: a SQLAlchemy Session is not thread-safe, and
    # handing one to six workers is a data race that would surface as
    # corrupted state long after the request that caused it.
    period_by_company = {
        (p.company or "").strip().lower(): p for p in (periods or [])
    }
    ctx = {
        "role": (requirement.parsed_role if requirement else "") or "the target role",
        "responsibilities": (
            "; ".join((requirement.responsibilities or [])[:6]) if requirement else "n/a"
        ),
    }

    # Decided here, once, across every project — see _assign_missing_skills.
    skills_for = _assign_missing_skills(content.projects, missing, period_by_company)
    if missing:
        logger.info(
            "JD skills to place: %s",
            "; ".join(
                f"{content.projects[i].name} <- {', '.join(v)}"
                for i, v in skills_for.items()
                if v
            )
            or "no project could host any of them",
        )

    owned_by_company: dict[str, list] = {}
    for project in content.projects:
        owned_by_company.setdefault((project.company or "").strip().lower(), []).append(project)

    units: list[tuple[str, object]] = []
    for index, project in enumerate(content.projects):
        period = period_by_company.get((project.company or "").strip().lower())
        units.append(("project", (index, project, period)))
    for block in content.experience:
        key = (block.company or "").strip().lower()
        units.append(("employer", (block, period_by_company.get(key), owned_by_company.get(key, []))))

    if not units:
        return {}

    def _run(unit):
        kind, args = unit
        if kind == "project":
            index, project, period = args
            return kind, index, _mould_one_project(
                provider, project, period, ctx, skills_for.get(index, [])
            )
        block, period, owned = args
        return kind, block, _mould_one_employer(provider, block, period, owned, ctx)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(len(units), MOULD_MAX_PARALLEL)) as pool:
        results = list(pool.map(_run, units))
    logger.info(
        "Moulding fan-out: %d units in %.1fs", len(units), time.monotonic() - started
    )

    # ── reassemble the payload _apply_mould already understands ────────
    payload: dict = {
        # The headline is the JD's own role. It was the one thing the mega-call
        # was asked for that needed no reasoning at all.
        "headline": ctx["role"],
        "projects": [],
        "experience": [],
        "unsupported_additions": [],
    }
    for kind, key, reply in results:
        if not reply:
            continue
        payload["unsupported_additions"] += [
            item for item in (reply.get("unsupported_additions") or []) if isinstance(item, dict)
        ]
        if kind == "project":
            project = content.projects[key]
            entry = {"ref": _project_ref(key, project)}
            for field in ("description", "responsibilities", "achievements"):
                if field in reply:
                    entry[field] = reply[field]
            payload["projects"].append(entry)
        else:
            entry = {"company": key.company}
            for field in ("description", "highlights"):
                if field in reply:
                    entry[field] = reply[field]
            payload["experience"].append(entry)

    if not payload["projects"] and not payload["experience"]:
        # Every unit failed. Same meaning as the old all-or-nothing failure:
        # version 1 will be the untransformed profile, and that is a visible
        # product failure rather than a detail.
        logger.error("Moulding fan-out produced nothing - version 1 is the raw profile")
        return {}
    return payload


def _jd_skill_names(db: Session, session: ResumeCompositionSession) -> tuple[set[str], set[str]]:
    """Lower-cased mandatory and preferred technology names for this JD."""
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    if requirement is None:
        return set(), set()
    mandatory: set[str] = set()
    preferred: set[str] = set()
    for link, tech in (
        db.query(RequirementTechnology, Technology)
        .join(Technology, Technology.id == RequirementTechnology.technology_id)
        .filter(RequirementTechnology.requirement_id == requirement.id)
        .all()
    ):
        (mandatory if link.importance == SkillImportance.MANDATORY else preferred).add(tech.name.lower())
    return mandatory, preferred


def _order_for_jd(content: ResumeContent, mandatory: set[str], preferred: set[str]) -> None:
    """Put what the JD asked for in front, without dropping anything.

    Deterministic rather than model-driven: ordering is arithmetic on the JD's
    own skill list, and a model asked to re-emit twenty skill groups will
    eventually drop one. Within a group the matching items lead; across groups,
    the ones carrying the most mandatory skills lead.
    """
    if not mandatory and not preferred:
        return

    def weight(name: str) -> int:
        low = name.lower()
        return 2 if low in mandatory else (1 if low in preferred else 0)

    for group in content.skill_groups:
        group.items = sorted(group.items, key=lambda i: (-weight(i), i.lower()))

    content.skill_groups.sort(
        key=lambda g: (
            0 if g.tier == "PRIMARY" else 1,
            -sum(weight(i) for i in g.items),
            g.label.lower(),
        )
    )

    # Projects that evidence the JD's stack come first; timelines are untouched.
    def project_weight(project) -> int:
        return sum(weight(t) for t in (project.technologies or []))

    content.projects.sort(key=lambda p: -project_weight(p))


def _apply_mould(content: ResumeContent, payload: dict) -> None:
    """Merge the model's moulded copy over the base truth.

    Only the fields the model is allowed to touch are read back. Employer
    NAMES, dates and the technology lists are taken from the pool regardless of
    what the reply contains, so a hallucinated employer cannot reach the resume
    even if the prompt is ignored.

    Which employer a project sits under IS the model's to decide — that is a
    narrative call, and an even split is rarely the best story — but the answer
    is resolved against the employers actually on this resume. A company name
    that matches none of them is ignored and the project keeps whatever
    assignment it already had.

    Project names are no longer read back either: the bank's name is the name.
    Renaming was how a resume started describing a system nobody built.
    """
    headline = payload.get("headline")
    if isinstance(headline, str) and headline.strip():
        content.contact.headline = headline.strip()

    # The summary is not read back — it is assembled afterward from one of the
    # standard formats (see summary_templates.py), not written freehand here.

    by_ref = {_project_ref(i, p): p for i, p in enumerate(content.projects)}
    for position, entry in enumerate(payload.get("projects") or []):
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("ref") or entry.get("source_project_id") or "")
        project = by_ref.get(ref)
        if project is None and position < len(content.projects):
            # Models drop the reference field more often than they reorder the
            # array, so position is a safer fallback than discarding the entry.
            project = content.projects[position]
        if project is None:
            continue
        # The employer this project prints under, resolved by name against the
        # employers on the resume. Never trusted blind: an unknown name is a
        # hallucination, and silently accepting it would put the project under
        # a company that is not on the document.
        wanted = str(entry.get("company") or "").strip().lower()
        if wanted:
            match = next(
                (e for e in content.experience if (e.company or "").strip().lower() == wanted), None
            )
            if match is not None:
                project.company = match.company
                project.source_company_id = match.source_company_id
            else:
                logger.info("Moulding named unknown employer %r for %r", wanted, project.name)

        # Not "role" — a project prints under its employer's designation only;
        # see the module docstring for why a second, per-project title is gone.
        value = entry.get("description")
        if isinstance(value, str) and value.strip():
            project.description = value.strip()
        for field in ("responsibilities", "achievements"):
            value = entry.get(field)
            if isinstance(value, list):
                cleaned = [str(x).strip() for x in value if str(x).strip()]
                if cleaned:
                    setattr(project, field, cleaned)

    # Designations, the employer's own description and its responsibilities may
    # all be written by the model; the employer they sit under may not.
    by_company = {(e.company or "").lower(): e for e in content.experience}
    for entry in payload.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        block = by_company.get(str(entry.get("company") or "").lower())
        if block is None:
            continue
        designation = entry.get("designation")
        if isinstance(designation, str) and designation.strip():
            block.designation = designation.strip()
        description = entry.get("description")
        if isinstance(description, str) and description.strip():
            block.description = description.strip()
        highlights = entry.get("highlights")
        if isinstance(highlights, list):
            cleaned = [str(x).strip() for x in highlights if str(x).strip()]
            if cleaned:
                block.highlights = cleaned


def summary_inputs_from(
    content: ResumeContent, *, jd_role: str | None, target_years: float | None, domain: str | None
) -> summary_templates.SummaryInputs:
    """The facts a summary template needs, read off the resume itself.

    Nothing here is generated: the role is what the header already prints,
    the skills are the ones already sorted JD-first in the skills section,
    and the highlight is the first achievement any project already claims.
    A template built from these can never say something the rest of the
    document does not already say.
    """
    role = (content.contact.headline or jd_role or "").strip() or "Engineer"
    skills: list[str] = []
    for group in content.skill_groups:
        for item in group.items:
            if item not in skills:
                skills.append(item)
        if len(skills) >= 3:
            break
    highlight = None
    for project in content.projects:
        if project.achievements:
            highlight = project.achievements[0]
            break
    if highlight is None:
        for project in content.projects:
            if project.responsibilities:
                highlight = project.responsibilities[0]
                break
    return summary_templates.SummaryInputs(
        role=role, years=target_years, skills=skills[:3], domain=domain, highlight=highlight
    )


def apply_summary_format(content: ResumeContent, format_index: int, inputs: summary_templates.SummaryInputs) -> None:
    """Render one of the standard formats and record that it has been used,
    so a later "try another one" in chat does not repeat it."""
    content.summary = summary_templates.render(format_index, inputs)
    tried = content.render_hints.setdefault("summary_templates_tried", [])
    if format_index not in tried:
        tried.append(format_index)


def _apply_honest_designations(
    periods: list[era.Period],
    content: ResumeContent,
    timeline: role_timeline_service.Timeline | None,
    target_years: float | None,
) -> None:
    """Set each employment's designation from its period — with variety.

    Most periods just take ``period.honest_role`` as computed. But the
    PREFIX of periods (most recent first) where the era already grants the
    JD's own literal title gets climbed as a ladder instead of repeated flat
    — see ``role_timeline.Timeline.climb``. A period earlier than that is
    already a genuine era demotion (a different, older title chronology
    forces), which is a fact, not a style choice, so it is left alone.

    Shared by generation and by ``relay_timeline``, so adding an employer or
    a project in chat re-lays this the same way version 1 did rather than
    collapsing back to one flat title everywhere.
    """
    # The discipline is what the era grants, so a period carrying it is one
    # the ladder may climb; a period carrying anything else was demoted by
    # chronology, which is a fact rather than a style choice.
    discipline = timeline.discipline if timeline else None
    eligible: list[era.Period] = []
    for period in periods:
        if period.honest_role and discipline and period.honest_role == discipline:
            eligible.append(period)
        else:
            break
    if eligible and timeline:
        # Oldest first, to match what varied_climb returns. Each period brings
        # its own start year, because how far back a title may reach is a fact
        # about that year rather than about its position in the list — and the
        # seed is the employer run, so this resume re-renders identically
        # while another built from the same JD gets its own progression.
        oldest_first = list(reversed(eligible))
        varied = timeline.varied_climb(
            [period.start_year for period in oldest_first],
            ceiling=role_timeline_service.ceiling_for(target_years),
            seed="|".join(period.company or "" for period in periods),
        )
        for period, title in zip(oldest_first, varied, strict=True):
            period.honest_role = title

    # The periods the era demoted are handled above by chronology alone, and
    # chronology can hand two neighbours the same answer: an employment
    # starting 2021 and one starting 2020 are both "AI Engineer" when the
    # JD's own title only arrived in 2022. Printing it twice in a row is the
    # other way this reads as generated, so the OLDER of the pair steps back
    # to the previous rung its own year supports. That is not a softer claim
    # than the one it replaces — every rung offered here had already emerged
    # by that year — it is simply the one a real career would have shown.
    if timeline is not None:
        for older, newer in zip(periods[1:], periods, strict=False):
            if not older.honest_role or older.honest_role != newer.honest_role:
                continue
            alternatives = [
                title
                for title in timeline.era_titles(older.start_year)
                if title != newer.honest_role
            ]
            if alternatives:
                older.honest_role = alternatives[-1]

    for period, block in zip(periods, content.experience, strict=True):
        if period.honest_role:
            block.designation = period.honest_role


def pair_projects_to_companies(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
) -> None:
    """Give every project an employer, as a fallback and a backstop.

    Which project belongs under which employer is the composer's call — it is
    a narrative decision, and an even split is rarely the best story. This is
    what runs when the model does not make that call: before it is asked (so
    the prompt can show a starting assignment) and after it replies (so a
    project the model forgot still has a home).

    Nothing in the bank is dated, so any project may sit under any employer;
    there is no constraint here to satisfy, only a gap to fill. The projects
    arrive ranked against the JD, so dealing them round-robin puts the
    strongest work under the employers that print first, and guarantees no
    employer is left as a bare heading.
    """
    employers = [e for e in content.experience if e.source_company_id]
    if not employers or not content.projects:
        return

    known = {e.source_company_id for e in employers}
    taken: dict[str, int] = {}
    for project in content.projects:
        if project.source_company_id in known:
            taken[project.source_company_id] = taken.get(project.source_company_id, 0) + 1

    for project in content.projects:
        if project.source_company_id in known:
            continue
        employer = min(employers, key=lambda e: taken.get(e.source_company_id or "", 0))
        taken[employer.source_company_id or ""] = taken.get(employer.source_company_id or "", 0) + 1
        project.company = employer.company
        project.source_company_id = employer.source_company_id

    # Print projects in their employers' order so a nested template reads
    # top-down like the experience section does.
    position = {e.source_company_id: i for i, e in enumerate(content.experience)}
    content.projects.sort(key=lambda p: position.get(p.source_company_id, 99))


def persist_project_companies(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> None:
    """Remember the pairing so a rebuild keeps this resume's own history."""
    rows = {
        str(p.project_id): p
        for p in db.query(ResumeSelectedProject).filter(ResumeSelectedProject.session_id == session.id)
        if p.project_id is not None
    }
    for project in content.projects:
        row = rows.get(project.source_project_id) if project.source_project_id else None
        if row is not None and project.source_company_id:
            row.company_id = UUID(project.source_company_id)


def rank_unused_projects(
    db: Session, session: ResumeCompositionSession, used_ids: set[str]
) -> list[Project]:
    """Bank projects not yet on this resume, best match for the JD first.

    The same ranking ``autoselect_for_session`` uses, exposed so that "add one
    more project" from the chat picks the genuine next-best rather than an
    arbitrary row — the 5th and 6th choices continue where the first four
    stopped. Projects already spent by an approved resume against this JD stay
    excluded, exactly as they are at generation.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = requirement.parsed_role if requirement else None
    jd_tokens = jd_fit.jd_context_tokens(requirement)
    spent, _ = used_projects_and_companies_for_requirement(
        db, session.requirement_id, exclude_session_id=session.id
    )
    blocked = {str(x) for x in spent} | {str(x) for x in used_ids}

    candidates = [
        p
        for p in db.query(Project).filter(Project.is_active.is_(True)).all()
        if str(p.id) not in blocked
    ]

    # Ranked over the CANDIDATES, so rarity is judged against what is still
    # available rather than the whole bank — after four picks the labels left
    # in the pool are what a fifth choice is competing against.
    role_counts = jd_fit.label_counts([p.role for p in candidates])
    domain = requirement.domain if requirement else None

    def score(project: Project) -> tuple[int, str]:
        return (
            -jd_project_score(
                project,
                jd_role=jd_role,
                domain=domain,
                jd_tokens=jd_tokens,
                role_counts=role_counts,
                pool_size=len(candidates),
            ),
            project.name.lower(),
        )

    return sorted(candidates, key=score)


def rank_unused_companies(
    db: Session, session: ResumeCompositionSession, used_ids: set[str]
) -> list[Company]:
    """Bank companies not yet on this resume, best match for the JD first."""
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    jd_role = requirement.parsed_role if requirement else None
    _, spent = used_projects_and_companies_for_requirement(
        db, session.requirement_id, exclude_session_id=session.id
    )
    blocked = {str(x) for x in spent} | {str(x) for x in used_ids}

    candidates = [
        c
        for c in db.query(Company).filter(Company.is_active.is_(True)).all()
        if str(c.id) not in blocked
    ]

    def score(company: Company) -> tuple[int, str]:
        return (-jd_fit.role_similarity(jd_role, company.role), company.name.lower())

    return sorted(candidates, key=score)


def project_block_from_bank(db: Session, project: Project) -> ProjectBlock:
    """A bank project as a resume block, with its recorded stack."""
    technologies = [
        tech.name
        for _, tech in db.query(ProjectTechnology, Technology)
        .join(Technology, Technology.id == ProjectTechnology.technology_id)
        .filter(ProjectTechnology.project_id == project.id)
        .all()
    ]
    return ProjectBlock(
        source_project_id=str(project.id),
        name=project.name,
        role=project.role,
        description=project.detailed_description or project.short_description or "",
        responsibilities=list(project.responsibilities or []),
        technologies=technologies,
    )


def reframe_added_project(
    db: Session,
    session: ResumeCompositionSession,
    block: ProjectBlock,
    period: era.Period | None,
    forbidden_terms: list[str] | None = None,
) -> bool:
    """Angle a freshly-added project at the JD, inside its period's limits.

    A project added from the chat arrives with the bank's own wording, which
    was written for nobody in particular and routinely names things that
    postdate the job it has just been placed under. The generation pass moulds
    every project this way; this is the same treatment for one project added
    later, so a resume does not end up half-moulded.

    Non-fatal: if the model is unreachable the project keeps its bank wording
    and the era check catches anything impossible. Returns whether it changed.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    provider = get_llm_provider()
    if provider.provider_name == "offline":
        return False

    # The terms this project ACTUALLY names that its period cannot support,
    # not a generic list of everything modern. A short, exact list is one the
    # model cannot skim past; the generic "newest 18" let a 2021 concept
    # through because a dozen 2023-24 names crowded it out.
    at_risk = list(forbidden_terms or [])
    if not at_risk and period and period.forbidden:
        at_risk = period.forbidden
    forbidden = ", ".join(at_risk) if at_risk else "nothing"
    window = (
        f"{period.start_year or '?'}-{period.end_label or 'Present'}" if period else "unknown"
    )
    user = (
        f"JOB DESCRIPTION\nRole: {requirement.parsed_role if requirement else 'n/a'}\n"
        f"Responsibilities: {'; '.join((requirement.responsibilities or [])[:6]) if requirement else 'n/a'}\n\n"
        f"THIS PROJECT SITS AT {block.company or 'an employer'} ({window})\n"
        f"NOT RELEASED YET IN THAT PERIOD, never mention: {forbidden}\n\n"
        f"PROJECT (name is fixed: {block.name})\n"
        f"Technologies on record: {', '.join(block.technologies) or 'n/a'}\n"
        f"Original description: {block.description[:900]}\n"
        f"Original bullets: {block.responsibilities[:8]}\n"
    )

    try:
        response = provider.chat(
            [{"role": "user", "content": user}],
            model=LLM_MODEL,
            system=PROJECT_REFRAME_SYSTEM.format(
                max_chars=RESUME_MAX_DESCRIPTION_CHARS,
                max_bullets=RESUME_MAX_BULLETS_PER_PROJECT,
            ),
            temperature=LLM_TEMPERATURE,
            max_tokens=max(LLM_MAX_TOKENS, 16000),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        payload = json.loads(extract_json(_strip_fences(response.text)))
    except (LLMTransientError, LLMPermanentError, json.JSONDecodeError, ValueError) as exc:
        logger.info("Reframe of %r skipped: %s", block.name, exc)
        return False

    changed = False
    description = payload.get("description")
    if isinstance(description, str) and description.strip():
        block.description = description.strip()
        changed = True
    bullets = payload.get("responsibilities")
    if isinstance(bullets, list):
        cleaned = [str(b).strip() for b in bullets if str(b).strip()]
        if cleaned:
            block.responsibilities = cleaned[:RESUME_MAX_BULLETS_PER_PROJECT]
            changed = True
    return changed


def relay_timeline(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> list[str]:
    """Re-lay the dates and era-honest titles after the shape changed.

    Adding or removing an employer changes how the JD's years divide, so the
    whole timeline is recomputed rather than patched — a new employer squeezed
    in beside untouched ones produces overlapping or impossible dates. Runs
    after any chat edit that changes which employers or projects are on the
    resume.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    target_years = requirement.min_experience_years if requirement else None
    jd_role = requirement.parsed_role if requirement else None

    # Clear the existing labels: meet_jd_experience only expands when the span
    # falls short, and the old dates already meet the target.
    for block in content.experience:
        block.start_label = None
        block.end_label = None

    ground_rules.meet_jd_experience(content, target_years)
    ground_rules.normalise_date_labels(content)

    releases = era.technology_release_years(db)
    timeline = role_timeline_service.resolve(db, jd_role)
    periods = era.periods_for(content, timeline, releases)
    _apply_honest_designations(periods, content, timeline, target_years)

    return strip_anachronistic_technologies(content, releases)


def refresh_designations(db: Session, session: ResumeCompositionSession, content: ResumeContent) -> bool:
    """Re-title every employment from its EXISTING dates — no shape change.

    What "the titles all look the same" or "give these more variety" asks
    for: the dates on the resume are left exactly where they are, but each
    designation is recomputed from era honesty plus the seniority ladder (see
    ``_apply_honest_designations``), so a flat run of one literal title
    becomes the graduated career a 5+-year resume should read as. Returns
    whether anything actually changed, since "give them variety" on a
    single-employer resume has nothing to vary.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    target_years = requirement.min_experience_years if requirement else None
    jd_role = requirement.parsed_role if requirement else None

    before = [block.designation for block in content.experience]
    releases = era.technology_release_years(db)
    timeline = role_timeline_service.resolve(db, jd_role)
    periods = era.periods_for(content, timeline, releases)
    _apply_honest_designations(periods, content, timeline, target_years)
    return [block.designation for block in content.experience] != before


def strip_anachronistic_technologies(
    content: ResumeContent, releases: dict[str, int]
) -> list[str]:
    """Drop technologies a project's period cannot support, from THIS resume.

    The bank is almost always modern: most projects on record name something
    released in the last two years. A JD asking eight years puts employers
    back in 2018, and a project placed there still carried its full bank
    technology list — so the resume printed "LangGraph" against a period three
    years before LangGraph existed. The prose was already constrained (the
    composer is given the forbidden list per period); the technology chips
    were not, because they are copied from the bank rather than written.

    Dropping them is a presentation decision, not an edit to the record: the
    project keeps every technology in the bank, this resume simply does not
    claim the ones its period cannot carry. Nothing is invented, and the
    project is never moved or removed to make the dates work.

    Returns one note per project trimmed, for the applied-rules report.
    """
    end_year: dict[str, int] = {
        e.source_company_id: era.end_year_of(e.end_label)
        for e in content.experience
        if e.source_company_id
    }

    notes: list[str] = []
    for project in content.projects:
        bound = end_year.get(project.source_company_id or "")
        if bound is None or not project.technologies:
            continue
        kept, dropped = [], []
        for name in project.technologies:
            released = releases.get(name.strip().lower())
            (dropped if released is not None and released > bound else kept).append(name)
        if not dropped:
            continue
        # Never strip a project down to nothing: a bare project with no stack
        # reads as an omission. If everything it names is too new for its
        # period, the era warning is the honest outcome and it is left to fire.
        if not kept:
            continue
        project.technologies = kept
        notes.append(
            f"'{project.name}': {', '.join(dropped)} not shown — "
            f"{'they were' if len(dropped) > 1 else 'it was'} released after this role's period."
        )
    return notes


def persist_composed_overrides(
    db: Session, session: ResumeCompositionSession, content: ResumeContent
) -> None:
    """Write the composed result back onto the rows a rebuild reads from.

    Moulding and the timeline expansion only ever touched the in-memory
    ``ResumeContent`` — nothing durable remembered what they decided. A
    rebuild (``rebuild_from_selections``, triggered by adding one project or
    nudging one company up a slot) reassembles the resume from
    ``build_pool_content`` alone, which reads these same override columns.
    Left empty, that reassembly silently reverted every OTHER company's
    designation and every project's moulded name and description back to raw,
    untransformed bank data — composing one thing looked like it had "reset"
    everything else.

    Only fills a gap: an override an operator already typed here is never
    touched, so this can never clobber a deliberate edit with a regenerate.
    """
    companies = {
        str(c.company_id): c
        for c in db.query(ResumeSelectedCompany).filter(ResumeSelectedCompany.session_id == session.id)
    }
    for block in content.experience:
        selection = companies.get(block.source_company_id) if block.source_company_id else None
        if selection is None:
            continue
        if not (selection.designation_override or "").strip() and block.designation:
            selection.designation_override = block.designation
        if not (selection.start_date_override or "").strip() and block.start_label:
            selection.start_date_override = block.start_label
        if not (selection.end_date_override or "").strip() and block.end_label:
            selection.end_date_override = block.end_label

    projects = {
        str(p.project_id): p
        for p in db.query(ResumeSelectedProject).filter(ResumeSelectedProject.session_id == session.id)
        if p.project_id is not None
    }
    for block in content.projects:
        selection = projects.get(block.source_project_id) if block.source_project_id else None
        if selection is None:
            continue
        if not (selection.title_override or "").strip() and block.name:
            selection.title_override = block.name
        if not (selection.role_override or "").strip() and block.role:
            selection.role_override = block.role
        if not (selection.description_override or "").strip() and block.description:
            selection.description_override = block.description
        if not selection.responsibilities_override and block.responsibilities:
            selection.responsibilities_override = list(block.responsibilities)
        if not selection.achievements_override and block.achievements:
            selection.achievements_override = list(block.achievements)


def persist_experience_dates(db: Session, session: ResumeCompositionSession, content: ResumeContent) -> None:
    """Write a fresh timeline expansion back onto the company rows it came from.

    Unlike ``persist_composed_overrides``, this overwrites rather than only
    filling a gap: it is called right after ``meet_jd_experience`` just
    recomputed every company's dates together, so the new values ARE the
    decision this time, not a default standing in for an empty field. Skipping
    this left the resume showing the freshly redistributed timeline while the
    Composition tab kept showing the stale one underneath it, and the next
    rebuild — seeing rows it thought were still undated — redistributed again
    from scratch instead of settling.
    """
    companies = {
        str(c.company_id): c
        for c in db.query(ResumeSelectedCompany).filter(ResumeSelectedCompany.session_id == session.id)
    }
    for block in content.experience:
        selection = companies.get(block.source_company_id) if block.source_company_id else None
        if selection is None:
            continue
        if block.start_label:
            selection.start_date_override = block.start_label
        if block.end_label:
            selection.end_date_override = block.end_label


@dataclass(slots=True)
class BaseComposition:
    """Version 1, plus the record of how it was produced."""

    content: ResumeContent
    unsupported: list[dict]
    fit: jd_fit.FitAssessment
    ground_rules: list[str]
    moulded: bool
    #: The pool content exactly as assembled, before the LLM touched it — None
    #: when nothing was moulded, since there is then nothing to compare against.
    #: Lets the groundedness judge (validation_engine) tell honest reframing
    #: apart from a rewrite that drifted into a different piece of work.
    source_content: ResumeContent | None = None


def generate_base_content(
    db: Session,
    session: ResumeCompositionSession,
    *,
    company_count: int | None = None,
    project_count: int | None = None,
    target_pages: int | None = None,
) -> BaseComposition:
    """Section 19 — produce the base resume.

    Two paths, chosen by how well the profile already matches the JD:

    * **fit at or above the threshold** — the profile IS the role being asked
      for. The stored history is transformed into the chosen template and
      nothing is re-described. Skills are still ordered so the JD's mandatory
      ones lead, because ordering asserts nothing new.
    * **below the threshold** — the profile is a different role. The history is
      moulded toward the JD first: job title, project names and framing, bullet
      vocabulary and designations all speak the target role's language.

    Either way the house ground rules then run (no education, no timeline bars,
    experience meeting the JD), so version 1 is consistent across every template
    that will ever be uploaded.

    The timeline is laid out BEFORE the model is called, not after. Moulding
    used to run against undated employment and the dates were filled in
    afterwards, which meant the model wrote every job as though it were
    happening today — a 2018 role described with LangGraph and RAG, titled "AI
    Engineer" three years before the title existed. It cannot honour a period
    it was never shown, so now it is shown one (see ``app/services/era.py``).
    """
    # What the operator asked for on the generation form drives the whole
    # composition: how much is fetched from the bank, how it is divided
    # between employers, and how much of the page budget each piece may spend.
    want_companies = company_count or AUTOSELECT_COMPANIES
    want_projects = min(project_count or AUTOSELECT_PROJECTS, RESUME_MAX_PROJECTS)

    progress.step(session.id, "Selecting projects and companies from the bank")
    autoselect_for_session(
        db, session, company_count=want_companies, project_count=want_projects
    )
    db.refresh(session)
    content = build_pool_content(db, session)

    progress.step(session.id, "Checking fit against the JD")
    fit = jd_fit.assess(db, session)
    unsupported: list[dict] = []
    source_content: ResumeContent | None = None

    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    target_years = requirement.min_experience_years if requirement else None
    jd_role = requirement.parsed_role if requirement else None

    # --- establish the timeline first, so everything below can honour it ------
    # Trim to the page before spreading the span, so the years land on the
    # employers that will actually print (the same order apply_v1_ground_rules
    # uses; it is re-run below and settles rather than redistributing again).
    progress.step(session.id, "Laying out the timeline")
    ground_rules.fit_one_page(
        content, max_projects=want_projects, max_companies=want_companies
    )

    # Date the employers first, so the model can see the periods it is
    # assigning work into. A starting assignment is filled in beneath, purely
    # so the prompt has something coherent to show; the model is free to
    # redraw it and usually does.
    releases = era.technology_release_years(db)
    expanded, previous_years = ground_rules.meet_jd_experience(content, target_years)
    ground_rules.normalise_date_labels(content)
    pair_projects_to_companies(db, session, content)

    timeline = role_timeline_service.resolve(db, jd_role)
    periods = era.periods_for(content, timeline, releases)

    # The era-honest, seniority-varied designation for each period, applied
    # before the model sees it. The prompt asks the model to keep these;
    # setting them here means the right answer is already on the page if it
    # ignores the instruction.
    _apply_honest_designations(periods, content, timeline, target_years)

    if fit.mould_required:
        progress.step(session.id, "Moulding the story to the JD")
        # Snapshot the pool content before the LLM sees it — the untouched base
        # truth the groundedness judge later compares the moulded copy against.
        source_content = content.clone()
        narrative = _llm_narrative(
            db, session, content, periods=periods
        )
        # Mould the base truth toward the JD before anyone sees it. This runs at
        # generation, not at render, so the studio preview, the DOCX and the
        # LaTeX all show the same adapted resume rather than three different
        # ones.
        _apply_mould(content, narrative)
        additions = narrative.get("unsupported_additions")
        if isinstance(additions, list):
            unsupported = [a for a in additions if isinstance(a, dict)]

    # The model decided which employer each project sits under; this fills in
    # anything it left out and re-sorts the projects into employer order.
    pair_projects_to_companies(db, session, content)

    # With the assignment settled, take off each project the technologies its
    # employer's period cannot carry. The prose was already constrained by the
    # timeline in the prompt; the technology list is copied from the bank and
    # is not written by anybody, so it has to be filtered here.
    stripped = strip_anachronistic_technologies(content, releases)

    mandatory, preferred = _jd_skill_names(db, session)
    _order_for_jd(content, mandatory, preferred)

    # Fall back to the JD's own title if the model declined to set one: when the
    # profile needed moulding, the stored designation is precisely the thing
    # this resume is meant to move away from.
    if fit.mould_required and not (content.contact.headline or "").strip():
        if jd_role:
            content.contact.headline = jd_role

    # The summary is WRITTEN, against the whole resume — see summary_writer.
    # It used to be assembled from an f-string template, which could not
    # hallucinate but also could not say anything: a label, a tech list, and
    # one sentence lifted verbatim out of a project bullet further down the
    # same page. The written one is checked before it is used, and the
    # templates remain the fallback when the model cannot be reached.
    progress.step(session.id, "Writing the summary")
    summary_inputs = summary_inputs_from(
        content, jd_role=jd_role, target_years=target_years, domain=requirement.domain if requirement else None
    )
    evidenced = {name.lower() for name in content.all_technologies()}
    written, _summary_model = summary_writer.write(
        content,
        role=jd_role or "",
        years=target_years,
        responsibilities=(requirement.responsibilities or []) if requirement else [],
        # Both are sets here; the prompt wants a stable order, and the
        # mandatory ones the candidate actually has come first.
        jd_skills=sorted(n for n in mandatory if n.lower() in evidenced) + sorted(preferred),
        missing_skills=sorted(n for n in mandatory if n.lower() not in evidenced),
    )
    if written:
        content.summary = written
    else:
        apply_summary_format(
            content, summary_templates.default_format_index(target_years), summary_inputs
        )

    # The model may have promoted an early role back to the target title
    # despite the timeline saying otherwise. The period is the fact; the
    # designation is presentation, so the period wins.
    for period, block in zip(periods, content.experience, strict=True):
        if period.honest_role and block.designation != period.honest_role:
            # The period already knows the honest title for its year; if the
            # model printed something else and that something is not a title
            # the year supports, the period wins.
            honest_now = timeline.title_for(period.start_year) if timeline else None
            if honest_now and block.designation != honest_now and period.honest_role != block.designation:
                logger.info(
                    "Designation %r at %s is not honest for %s; reset to %r",
                    block.designation, block.company, period.start_year, period.honest_role,
                )
                block.designation = period.honest_role

    progress.step(session.id, "Applying house rules")
    # The dates were already laid out above; this pass applies the remaining
    # house rules and must not redistribute them a second time.
    rules = ground_rules.apply_v1_ground_rules(
        content,
        target_experience_years=target_years,
        keep_experience_dates=True,
        max_projects=want_projects,
        max_companies=want_companies,
    )
    if target_pages:
        content.render_hints["max_pages"] = target_pages
    # The expansion happened above rather than inside that call, so its note
    # has to be added here — it is what the studio shows the operator to
    # explain why the dates are what they are.
    if expanded and target_years:
        count = len(content.experience)
        rules.applied.append(
            f"Experience timeline expanded from ~{previous_years or 0:g} to {float(target_years):g} "
            f"years across the {count} compan{'y' if count == 1 else 'ies'} already on the resume."
        )
    era_notes = [
        f"{p.company}: designation set to '{p.honest_role}' — the period starts {p.start_year}."
        for p in periods
        if p.honest_role and jd_role and p.honest_role != jd_role and p.start_year
    ]
    rules.applied.extend(era_notes)
    rules.applied.extend(stripped)

    # Now that the content is final, remember it on the selection rows a
    # rebuild reads from — see persist_composed_overrides for why.
    persist_project_companies(db, session, content)
    persist_composed_overrides(db, session, content)

    session.fit_score = fit.score
    session.fit_basis = fit.basis

    return BaseComposition(
        content=content,
        unsupported=unsupported,
        fit=fit,
        ground_rules=rules.applied,
        moulded=fit.mould_required,
        source_content=source_content,
    )
