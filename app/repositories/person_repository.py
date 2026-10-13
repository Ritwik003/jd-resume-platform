"""Reads and writes for people, their profiles and their employment history.

Section 10/11 shape: a Person is the identity, a PersonProfile is that identity
offered as one role, and an Employment is a company plus the title held there.
Callers ask for the thing they mean — "the profiles this person has", "the
employers on record" — rather than assembling the join themselves.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.person import Employment, Person, PersonProfile, ProfileType


# ─────────────────────────────────────────────────────────────────── person
def get(db: Session, person_id: UUID) -> Person | None:
    return db.query(Person).filter(Person.id == person_id).first()


def get_active(db: Session, person_id: UUID) -> Person | None:
    return (
        db.query(Person)
        .filter(Person.id == person_id, Person.is_active.is_(True))
        .first()
    )


def find_by_name(db: Session, full_name: str) -> Person | None:
    """Case-insensitive exact match, used to spot a re-import of the same CV."""
    return (
        db.query(Person)
        .filter(Person.full_name.ilike(full_name.strip()), Person.is_active.is_(True))
        .first()
    )


def list_active(db: Session) -> list[Person]:
    return db.query(Person).filter(Person.is_active.is_(True)).all()


# ────────────────────────────────────────────────────────────────── profile
def get_profile(db: Session, profile_id: UUID) -> PersonProfile | None:
    return db.query(PersonProfile).filter(PersonProfile.id == profile_id).first()


def profiles_for_person(db: Session, person_id: UUID) -> list[PersonProfile]:
    return db.query(PersonProfile).filter(PersonProfile.person_id == person_id).all()


def profile_for_type(
    db: Session, person_id: UUID, profile_type_id: UUID
) -> PersonProfile | None:
    """Section 10 — one profile per person per role; this is that uniqueness."""
    return (
        db.query(PersonProfile)
        .filter(
            PersonProfile.person_id == person_id,
            PersonProfile.profile_type_id == profile_type_id,
        )
        .first()
    )


# ───────────────────────────────────────────────────────────── profile type
def get_profile_type(db: Session, profile_type_id: UUID) -> ProfileType | None:
    return db.query(ProfileType).filter(ProfileType.id == profile_type_id).first()


def find_profile_type_by_name(db: Session, name: str) -> ProfileType | None:
    return db.query(ProfileType).filter(ProfileType.name.ilike(name.strip())).first()


# ─────────────────────────────────────────────────────────────── employment
def employments_for_person(db: Session, person_id: UUID) -> list[Employment]:
    return db.query(Employment).filter(Employment.person_id == person_id).all()


def employments_newest_first(db: Session, person_id: UUID) -> list[Employment]:
    return (
        db.query(Employment)
        .filter(Employment.person_id == person_id)
        .order_by(Employment.created_at.desc())
        .all()
    )
