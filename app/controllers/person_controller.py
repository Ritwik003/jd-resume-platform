"""/profiles — sections 10/11/13 (person identity, profile types, technologies)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.company import Company
from app.models.person import Employment, Person, PersonProfile, ProfileType
from app.models.project import Project
from app.models.technology import PersonTechnology, Technology
from app.models.user import User
from app.repositories.base_repository import apply_updates, get_or_404, list_and_count
from app.schemas.availability import ProfileVendorStatusResponse
from app.schemas.common import MessageResponse, Page, PageMeta
from app.schemas.profiles import (
    EmploymentCreate,
    EmploymentResponse,
    PersonCreate,
    PersonProfileCreate,
    PersonProfileResponse,
    PersonProfileUpdate,
    PersonResponse,
    PersonTechnologyCreate,
    PersonTechnologyResponse,
    PersonUpdate,
)
from app.services import availability
from app.utils.exceptions import ConflictError, NotFoundError
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


# ------------------------------------------------------------------- persons
@router.get("", response_model=Page[PersonResponse])
def list_persons(
    search: str | None = Query(None),
    include_inactive: bool = Query(False),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[PersonResponse]:
    query = db.query(Person)
    if not include_inactive:
        query = query.filter(Person.is_active.is_(True))
    if search:
        query = query.filter(Person.full_name.ilike(f"%{search}%"))
    items, total = list_and_count(query.order_by(Person.full_name), page.skip, page.limit)
    return Page(
        items=[PersonResponse.model_validate(i) for i in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=PersonResponse, status_code=status.HTTP_201_CREATED)
def create_person(
    payload: PersonCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Person:
    data = payload.model_dump()
    if data.get("email"):
        data["email"] = str(data["email"]).lower()
        if db.query(Person).filter(Person.email == data["email"]).first():
            raise ConflictError("A person with this email already exists")
    person = Person(**data)
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


@router.get("/{person_id}", response_model=PersonResponse)
def get_person(person_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> Person:
    return get_or_404(db, Person, person_id, "Person")


@router.patch("/{person_id}", response_model=PersonResponse)
def update_person(
    person_id: UUID, payload: PersonUpdate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Person:
    person = get_or_404(db, Person, person_id, "Person")
    apply_updates(person, payload)
    db.commit()
    db.refresh(person)
    return person


# -------------------------------------------------------------- profile types
@router.get("/{person_id}/profiles", response_model=list[PersonProfileResponse])
def list_person_profiles(
    person_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[PersonProfile]:
    get_or_404(db, Person, person_id, "Person")
    return (
        db.query(PersonProfile)
        .filter(PersonProfile.person_id == person_id, PersonProfile.is_active.is_(True))
        .all()
    )


@router.post("/{person_id}/profiles", response_model=PersonProfileResponse, status_code=status.HTTP_201_CREATED)
def create_person_profile(
    person_id: UUID,
    payload: PersonProfileCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PersonProfile:
    """Section 10 — a new role view of the same identity, never a duplicate person."""
    get_or_404(db, Person, person_id, "Person")
    get_or_404(db, ProfileType, payload.profile_type_id, "Profile type")

    existing = (
        db.query(PersonProfile)
        .filter(
            PersonProfile.person_id == person_id,
            PersonProfile.profile_type_id == payload.profile_type_id,
        )
        .first()
    )
    if existing is not None:
        raise ConflictError("This person already has a profile of that type")

    profile = PersonProfile(
        person_id=person_id,
        profile_type_id=payload.profile_type_id,
        title=payload.title,
        summary=payload.summary,
        headline_achievements=payload.headline_achievements,
    )
    if payload.project_ids:
        profile.projects = db.query(Project).filter(Project.id.in_(payload.project_ids)).all()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.patch("/{person_id}/profiles/{profile_id}", response_model=PersonProfileResponse)
def update_person_profile(
    person_id: UUID,
    profile_id: UUID,
    payload: PersonProfileUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PersonProfile:
    profile = (
        db.query(PersonProfile)
        .filter(PersonProfile.id == profile_id, PersonProfile.person_id == person_id)
        .first()
    )
    if profile is None:
        raise NotFoundError("Profile not found for this person")
    if payload.project_ids is not None:
        profile.projects = db.query(Project).filter(Project.id.in_(payload.project_ids)).all()
    apply_updates(profile, payload, exclude={"project_ids"})
    db.commit()
    db.refresh(profile)
    return profile


# -------------------------------------------------------- person technologies
@router.get("/{person_id}/technologies", response_model=list[PersonTechnologyResponse])
def list_person_technologies(
    person_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[PersonTechnologyResponse]:
    get_or_404(db, Person, person_id, "Person")
    rows = (
        db.query(PersonTechnology, Technology)
        .join(Technology, Technology.id == PersonTechnology.technology_id)
        .filter(PersonTechnology.person_id == person_id)
        .order_by(Technology.name)
        .all()
    )
    return [
        PersonTechnologyResponse(
            **{**PersonTechnologyResponse.model_validate(link).model_dump(), "technology_name": tech.name}
        )
        for link, tech in rows
    ]


@router.put("/{person_id}/technologies", response_model=PersonTechnologyResponse)
def upsert_person_technology(
    person_id: UUID,
    payload: PersonTechnologyCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PersonTechnologyResponse:
    """Section 13 — experience level is stored per person, not on the master."""
    get_or_404(db, Person, person_id, "Person")
    technology = get_or_404(db, Technology, payload.technology_id, "Technology")

    link = (
        db.query(PersonTechnology)
        .filter(
            PersonTechnology.person_id == person_id,
            PersonTechnology.technology_id == payload.technology_id,
        )
        .first()
    )
    if link is None:
        link = PersonTechnology(person_id=person_id, technology_id=payload.technology_id)
        db.add(link)

    link.years_of_experience = payload.years_of_experience
    link.proficiency = payload.proficiency.value if payload.proficiency else None
    link.is_primary = "true" if payload.is_primary else "false"
    link.notes = payload.notes
    db.commit()
    db.refresh(link)
    return PersonTechnologyResponse(
        **{**PersonTechnologyResponse.model_validate(link).model_dump(), "technology_name": technology.name}
    )


@router.delete("/{person_id}/technologies/{technology_id}", response_model=MessageResponse)
def remove_person_technology(
    person_id: UUID,
    technology_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MessageResponse:
    deleted = (
        db.query(PersonTechnology)
        .filter(PersonTechnology.person_id == person_id, PersonTechnology.technology_id == technology_id)
        .delete()
    )
    db.commit()
    if not deleted:
        raise NotFoundError("That technology is not recorded against this person")
    return MessageResponse(message="Technology removed from the person's record")


# ---------------------------------------------------------------- employments
@router.get("/{person_id}/employments", response_model=list[EmploymentResponse])
def list_employments(
    person_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[EmploymentResponse]:
    get_or_404(db, Person, person_id, "Person")
    rows = (
        db.query(Employment, Company)
        .join(Company, Company.id == Employment.company_id)
        .filter(Employment.person_id == person_id)
        # No stored dates to order by: the bank records that somebody worked
        # somewhere, never when. Newest-first is decided per resume.
        .order_by(Employment.created_at.desc())
        .all()
    )
    return [
        EmploymentResponse(
            **{**EmploymentResponse.model_validate(e).model_dump(), "company_name": c.name}
        )
        for e, c in rows
    ]


@router.post("/{person_id}/employments", response_model=EmploymentResponse, status_code=status.HTTP_201_CREATED)
def create_employment(
    person_id: UUID,
    payload: EmploymentCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> EmploymentResponse:
    get_or_404(db, Person, person_id, "Person")
    company = get_or_404(db, Company, payload.company_id, "Company")
    employment = Employment(
        person_id=person_id,
        company_id=payload.company_id,
        designation=payload.designation,
        location=payload.location,
    )
    db.add(employment)
    db.commit()
    db.refresh(employment)
    return EmploymentResponse(
        **{**EmploymentResponse.model_validate(employment).model_dump(), "company_name": company.name}
    )


# ------------------------------------------------------------- vendor history
@router.get("/{person_id}/vendor-status", response_model=list[ProfileVendorStatusResponse])
def person_vendor_status(
    person_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[ProfileVendorStatusResponse]:
    """Section 29 — the same person, tracked independently per vendor."""
    get_or_404(db, Person, person_id, "Person")
    availability.expire_stale_reservations(db)
    db.commit()
    return [
        ProfileVendorStatusResponse.model_validate(row)
        for row in availability.vendor_history_for_person(db, person_id)
    ]
