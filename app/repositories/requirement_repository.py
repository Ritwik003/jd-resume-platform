"""Reads for the parsed JD and the skills it asks for (sections 5, 14).

``mandatory_and_preferred`` is the query the whole platform turns on: the fit
score, the skill ordering and the moulding prompt all need the same split, and
each computing it separately is how they drift apart.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.requirement import Requirement, RequirementTechnology
from app.models.technology import Technology
from app.utils.enums import SkillImportance


def get(db: Session, requirement_id: UUID) -> Requirement | None:
    return db.query(Requirement).filter(Requirement.id == requirement_id).first()


def technology_links(
    db: Session, requirement_id: UUID
) -> list[tuple[RequirementTechnology, Technology | None]]:
    """Every skill row, with its master technology when one resolved."""
    return (
        db.query(RequirementTechnology, Technology)
        .outerjoin(Technology, Technology.id == RequirementTechnology.technology_id)
        .filter(RequirementTechnology.requirement_id == requirement_id)
        .all()
    )


def mandatory_and_preferred(db: Session, requirement_id: UUID) -> tuple[list[str], list[str]]:
    """The JD's skill names, split by importance, in the order they were parsed."""
    mandatory: list[str] = []
    preferred: list[str] = []
    for link, tech in technology_links(db, requirement_id):
        name = (tech.name if tech else getattr(link, "raw_name", None)) or ""
        if not name.strip():
            continue
        target = mandatory if link.importance == SkillImportance.MANDATORY else preferred
        target.append(name.strip())
    return mandatory, preferred


def mandatory_and_preferred_ids(
    db: Session, requirement_id: UUID
) -> tuple[set[UUID], set[UUID]]:
    """Technology ids rather than names, for tiering the session's skill list."""
    jd_ids: set[UUID] = set()
    mandatory_ids: set[UUID] = set()
    for row in db.query(RequirementTechnology).filter(
        RequirementTechnology.requirement_id == requirement_id
    ):
        if row.technology_id:
            jd_ids.add(row.technology_id)
            if row.importance == SkillImportance.MANDATORY:
                mandatory_ids.add(row.technology_id)
    return jd_ids, mandatory_ids


def first_release_dates(db: Session, requirement_id: UUID) -> dict[str, str]:
    """Release dates for the JD's technologies, so the composer can honour them."""
    return {
        tech.name: tech.first_release_date.isoformat()
        for _, tech in technology_links(db, requirement_id)
        if tech and tech.first_release_date
    }
