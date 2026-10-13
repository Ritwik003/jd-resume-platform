"""Reads for the technology master and per-person depth (sections 12 and 13).

The name/alias index matters more than it looks: a resume saying "Postgres",
"postgresql" or "py" has to land on the existing master row, or the bank fills
up with near-duplicates that never match a JD.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.technology import PersonTechnology, Technology


def get(db: Session, technology_id: UUID) -> Technology | None:
    return db.query(Technology).filter(Technology.id == technology_id).first()


def list_all(db: Session) -> list[Technology]:
    return db.query(Technology).all()


def index_by_name_and_alias(db: Session) -> dict[str, Technology]:
    """Lower-cased name and alias -> Technology, for import-time resolution."""
    index: dict[str, Technology] = {}
    for tech in db.query(Technology).all():
        if tech.name:
            index[tech.name.strip().lower()] = tech
        for alias in tech.aliases or []:
            index.setdefault(str(alias).strip().lower(), tech)
    return index


def known_names(db: Session) -> set[str]:
    """Every name and alias, lower-cased — what "is this a real skill" means."""
    known: set[str] = set()
    for tech in db.query(Technology).all():
        if tech.name:
            known.add(tech.name.strip().lower())
        for alias in tech.aliases or []:
            known.add(str(alias).strip().lower())
    return known


def release_dates(db: Session) -> dict[str, object]:
    """Lower-cased name -> first release date, for the timeline check (section 25)."""
    return {
        tech.name.lower(): tech.first_release_date
        for tech in db.query(Technology).filter(Technology.first_release_date.isnot(None)).all()
    }


# ─────────────────────────────────────────────── per-person depth (section 13)
def person_links(db: Session, person_id: UUID) -> list[PersonTechnology]:
    return db.query(PersonTechnology).filter(PersonTechnology.person_id == person_id).all()


def person_links_with_technology(
    db: Session, person_id: UUID
) -> list[tuple[PersonTechnology, Technology]]:
    return (
        db.query(PersonTechnology, Technology)
        .join(Technology, Technology.id == PersonTechnology.technology_id)
        .filter(PersonTechnology.person_id == person_id)
        .all()
    )


def person_technology_names(db: Session, person_id: UUID) -> list[str]:
    return [tech.name for _, tech in person_links_with_technology(db, person_id)]


def person_technology_names_lower(db: Session, person_id: UUID) -> set[str]:
    return {
        tech.name.strip().lower()
        for _, tech in person_links_with_technology(db, person_id)
        if tech and tech.name
    }
