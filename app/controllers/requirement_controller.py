"""/requirements — sections 5, 14, 32 (paste JD, parse, store, correct)."""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.requirement import Requirement, RequirementTechnology
from app.models.resume_session import ResumeCompositionSession
from app.models.resume_template import ResumeTemplate
from app.models.technology import Technology
from app.models.user import User
from app.models.vendor import Vendor
from app.repositories.base_repository import get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.requirements import (
    AddTechnologiesRequest,
    JDParsePreviewResponse,
    JDParseRequest,
    MissingTechnologiesResponse,
    MissingTechnology,
    ParsedJD,
    RequirementCreate,
    RequirementResponse,
    RequirementTechnologyResponse,
    RequirementUpdate,
)
from app.services import role_timeline, roles, technology_knowledge
from app.services.jd_parser import parse_and_resolve, resolve_skills
from app.utils.enums import RequirementStatus, SkillImportance
from app.utils.exceptions import ConflictError
from app.utils.helper import allocate_reference_code
from app.utils.pagination import Pagination, pagination_params

logger = logging.getLogger(__name__)

router = APIRouter()


def _prewarm_role_timeline(db: Session, requirement: Requirement) -> None:
    """Resolve this JD's role dating now, while the operator is already
    waiting on the parse, instead of later inside reserve-and-generate.

    generate_base_resume calls role_timeline.resolve() to know which titles
    are honest for which years -- and on the first time a given role is ever
    seen, that is a real model call, sitting inline in the one flow every
    user is impatient about. Resolving is cached per role (see
    app/services/role_timeline.py), so calling it here costs nothing on a
    cache hit and moves the cache-miss cost off the generate critical path
    entirely, onto a step that already involves waiting on a model call.
    Never allowed to fail the JD parse itself.
    """
    if not (requirement.parsed_role or "").strip():
        return
    try:
        role_timeline.resolve(db, requirement.parsed_role)
        db.commit()
    except Exception:  # noqa: BLE001 -- pre-warming must never break JD parsing
        logger.exception("Could not pre-warm the role timeline for %r", requirement.parsed_role)
        db.rollback()


def _to_response(db: Session, requirement: Requirement) -> RequirementResponse:
    vendor = db.query(Vendor).filter(Vendor.id == requirement.vendor_id).first()
    template = (
        db.query(ResumeTemplate).filter(ResumeTemplate.id == requirement.template_id).first()
        if requirement.template_id
        else None
    )
    rows = (
        db.query(RequirementTechnology, Technology)
        .outerjoin(Technology, Technology.id == RequirementTechnology.technology_id)
        .filter(RequirementTechnology.requirement_id == requirement.id)
        .all()
    )
    return RequirementResponse(
        **{
            **RequirementResponse.model_validate(requirement).model_dump(exclude={"technologies"}),
            "vendor_name": vendor.name if vendor else None,
            "template_name": template.name if template else None,
            "technologies": [
                RequirementTechnologyResponse(
                    technology_id=link.technology_id,
                    technology_name=tech.name if tech else link.raw_label,
                    importance=link.importance,
                    raw_label=link.raw_label,
                )
                for link, tech in rows
            ],
        }
    )


def _write_skills(db: Session, requirement: Requirement, parsed: ParsedJD) -> None:
    """Section 14 — importance is stored on the requirement, never on the master."""
    db.query(RequirementTechnology).filter(RequirementTechnology.requirement_id == requirement.id).delete()
    resolved, _ = resolve_skills(db, parsed)
    seen: set[str] = set()
    unmatched_mandatory: list[str] = []
    unmatched_preferred: list[str] = []
    for skill in resolved:
        key = str(skill.technology_id or skill.raw_label.lower())
        if key in seen:
            continue
        seen.add(key)
        if skill.technology_id is None:
            # A JD skill that matches no technology row at all — kept as a raw
            # label on the requirement itself so it can be warned about, since
            # it can never become a RequirementTechnology row.
            (
                unmatched_mandatory
                if skill.importance == SkillImportance.MANDATORY
                else unmatched_preferred
            ).append(skill.raw_label)
            continue
        db.add(
            RequirementTechnology(
                requirement_id=requirement.id,
                technology_id=skill.technology_id,
                importance=skill.importance.value,
                raw_label=skill.raw_label,
            )
        )
    requirement.unmatched_mandatory_skills = unmatched_mandatory
    requirement.unmatched_preferred_skills = unmatched_preferred


def _apply_parsed(db: Session, requirement: Requirement, parsed: ParsedJD) -> None:
    # Section 10 — the role the JD asks for is the role vocabulary. A name we
    # already hold is reused, a new one joins the pool, so profile types are a
    # record of what we have been asked for rather than a list to curate.
    roles.register_role(db, parsed.role)

    requirement.parsed_role = parsed.role
    requirement.min_experience_years = parsed.min_experience_years
    requirement.max_experience_years = parsed.max_experience_years
    requirement.location = parsed.location
    requirement.domain = parsed.domain
    requirement.responsibilities = parsed.responsibilities
    requirement.other_requirements = parsed.other_requirements
    requirement.parsed_payload = parsed.model_dump(mode="json")
    requirement.is_parsed = "true"


@router.get("/{requirement_id}/missing-technologies", response_model=MissingTechnologiesResponse)
def missing_technologies(
    requirement_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> MissingTechnologiesResponse:
    """JD skills the technology master has no row for.

    Asked by the generation form. A skill here cannot be selected onto a
    resume, cannot print in the skills section and cannot be era-checked, so
    the operator is given the chance to add it before composing rather than
    discovering the gap in a warning afterwards.
    """
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")
    missing: list[MissingTechnology] = []
    for names, importance in (
        (requirement.unmatched_mandatory_skills or [], SkillImportance.MANDATORY.value),
        (requirement.unmatched_preferred_skills or [], SkillImportance.PREFERRED.value),
    ):
        for raw in names:
            name = str(raw).strip()
            if not name:
                continue
            missing.append(
                MissingTechnology(
                    name=name,
                    importance=importance,
                    suggested_release_year=technology_knowledge.suggested_release_year(name),
                )
            )
    return MissingTechnologiesResponse(requirement_id=requirement.id, missing=missing)


@router.post("/{requirement_id}/technologies", response_model=RequirementResponse)
def add_missing_technologies(
    requirement_id: UUID,
    payload: AddTechnologiesRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RequirementResponse:
    """Add approved technologies to the master, then re-resolve this JD's skills.

    Two steps that have to happen together. Creating the rows is not enough on
    its own: the requirement's skills were resolved when the JD was parsed and
    these names failed to match, so without re-resolving they stay in the
    unmatched list and never reach the resume. Re-running the resolution is
    what turns them into real links, which is what puts them in the skills
    section and makes them selectable.

    Every technology gets a release date — that date is the ground truth the
    era checks compare against, and one without it opts out of them silently.
    Duplicates are skipped rather than re-added, matched on name AND alias.
    """
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")

    taken: set[str] = set()
    for tech in db.query(Technology).all():
        if tech.name:
            taken.add(tech.name.strip().lower())
        for alias in tech.aliases or []:
            taken.add(str(alias).strip().lower())

    for item in payload.technologies:
        name = item.name.strip()
        if not name or name.lower() in taken:
            continue
        db.add(
            Technology(
                name=name,
                category=item.category or technology_knowledge.suggested_category(name),
                first_release_date=date(item.release_year, item.release_month, 1),
            )
        )
        taken.add(name.lower())
    db.flush()

    # Re-resolve against the enlarged master so the newly added rows become
    # real requirement links rather than staying in the unmatched list.
    parsed = ParsedJD.model_validate(requirement.parsed_payload or {})
    _write_skills(db, requirement, parsed)
    db.commit()
    return _to_response(db, requirement)


@router.post("/parse-preview", response_model=JDParsePreviewResponse)
def parse_preview(
    payload: JDParseRequest, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> JDParsePreviewResponse:
    """Section 5 — parse without saving, so the user can review before continuing."""
    result = parse_and_resolve(db, payload.raw_jd_text)
    return JDParsePreviewResponse(
        parsed=result.parsed,
        resolved_skills=result.resolved,
        unmatched_skills=result.unmatched,
        model=result.model,
    )


@router.get("", response_model=Page[RequirementResponse])
def list_requirements(
    vendor_id: UUID | None = Query(None),
    status_filter: RequirementStatus | None = Query(None, alias="status"),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[RequirementResponse]:
    query = db.query(Requirement)
    if vendor_id:
        query = query.filter(Requirement.vendor_id == vendor_id)
    if status_filter:
        query = query.filter(Requirement.status == status_filter.value)
    items, total = list_and_count(query.order_by(Requirement.created_at.desc()), page.skip, page.limit)
    return Page(
        items=[_to_response(db, r) for r in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=RequirementResponse, status_code=status.HTTP_201_CREATED)
def create_requirement(
    payload: RequirementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RequirementResponse:
    """Section 32 — every JD becomes a stored Requirement; the raw text is kept."""
    get_or_404(db, Vendor, payload.vendor_id, "Vendor")
    if payload.template_id is not None:
        get_or_404(db, ResumeTemplate, payload.template_id, "Resume template")

    requirement = Requirement(
        reference_code=allocate_reference_code(db, Requirement.reference_code, "REQ"),
        vendor_id=payload.vendor_id,
        template_id=payload.template_id,
        raw_jd_text=payload.raw_jd_text,
        status=RequirementStatus.OPEN,
        created_by_id=current_user.id,
    )
    db.add(requirement)
    db.flush()

    if payload.parsed is not None:
        parsed = payload.parsed
        requirement.is_confirmed = "true"
    else:
        result = parse_and_resolve(db, payload.raw_jd_text)
        parsed = result.parsed
        requirement.parse_model = result.model

    _apply_parsed(db, requirement, parsed)
    _write_skills(db, requirement, parsed)
    db.commit()
    db.refresh(requirement)
    _prewarm_role_timeline(db, requirement)
    return _to_response(db, requirement)


@router.get("/{requirement_id}", response_model=RequirementResponse)
def get_requirement(
    requirement_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> RequirementResponse:
    return _to_response(db, get_or_404(db, Requirement, requirement_id, "Requirement"))


@router.patch("/{requirement_id}", response_model=RequirementResponse)
def update_requirement(
    requirement_id: UUID,
    payload: RequirementUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RequirementResponse:
    """Section 5 — the parsed information can be corrected before continuing."""
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")

    # An explicit parse block always wins — that is the human correcting the
    # machine. New JD text without one means "re-read this", so we re-parse.
    if payload.parsed is not None:
        _apply_parsed(db, requirement, payload.parsed)
        _write_skills(db, requirement, payload.parsed)
        requirement.is_confirmed = "true"
    elif payload.raw_jd_text is not None and payload.raw_jd_text != requirement.raw_jd_text:
        requirement.raw_jd_text = payload.raw_jd_text
        result = parse_and_resolve(db, payload.raw_jd_text)
        requirement.parse_model = result.model
        _apply_parsed(db, requirement, result.parsed)
        _write_skills(db, requirement, result.parsed)
        requirement.is_confirmed = "false"
    if payload.template_id is not None:
        get_or_404(db, ResumeTemplate, payload.template_id, "Resume template")
        requirement.template_id = payload.template_id
    if payload.status is not None:
        requirement.status = payload.status.value
    if payload.is_confirmed is not None:
        requirement.is_confirmed = "true" if payload.is_confirmed else "false"
    db.commit()
    db.refresh(requirement)
    _prewarm_role_timeline(db, requirement)
    return _to_response(db, requirement)


@router.post("/{requirement_id}/reparse", response_model=RequirementResponse)
def reparse_requirement(
    requirement_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> RequirementResponse:
    """Re-run the parser against the retained original JD (section 32)."""
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")
    result = parse_and_resolve(db, requirement.raw_jd_text)
    _apply_parsed(db, requirement, result.parsed)
    _write_skills(db, requirement, result.parsed)
    requirement.parse_model = result.model
    requirement.is_confirmed = "false"
    db.commit()
    db.refresh(requirement)
    _prewarm_role_timeline(db, requirement)
    return _to_response(db, requirement)


@router.delete("/{requirement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_requirement(
    requirement_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> None:
    """Remove a JD that was never worked on.

    Refused once a resume session exists against it: those sessions carry
    versions, submissions and vendor history (sections 27–28), and deleting the
    requirement underneath them would strand that record. Close the requirement
    instead.
    """
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")
    session_count = (
        db.query(ResumeCompositionSession)
        .filter(ResumeCompositionSession.requirement_id == requirement_id)
        .count()
    )
    if session_count:
        raise ConflictError(
            f"This JD has {session_count} resume session(s) against it. "
            "Set its status to CLOSED instead of deleting it."
        )
    db.query(RequirementTechnology).filter(
        RequirementTechnology.requirement_id == requirement_id
    ).delete()
    db.delete(requirement)
    db.commit()
