"""Reads and writes for the reusable project pool (section 15).

Projects carry no person or company of their own — every one is a standalone
capability record, matched to a JD independently — so what callers need here
is the technology stack behind a project or a set of them.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.project import Project, ProjectTechnology
from app.models.technology import Technology


def get(db: Session, project_id: UUID) -> Project | None:
    return db.query(Project).filter(Project.id == project_id).first()


def technology_names(db: Session, project_id: UUID) -> list[str]:
    return [
        tech.name
        for _, tech in db.query(ProjectTechnology, Technology)
        .join(Technology, Technology.id == ProjectTechnology.technology_id)
        .filter(ProjectTechnology.project_id == project_id)
        .all()
    ]


def technology_ids_by_project(db: Session, project_ids: list[UUID]) -> dict[UUID, set[UUID]]:
    """One query for many projects, so ranking a pool is not N+1."""
    grouped: dict[UUID, set[UUID]] = {}
    if not project_ids:
        return grouped
    for link in db.query(ProjectTechnology).filter(
        ProjectTechnology.project_id.in_(project_ids)
    ):
        grouped.setdefault(link.project_id, set()).add(link.technology_id)
    return grouped


def technology_names_for_projects(db: Session, project_ids: list[UUID]) -> set[str]:
    """Lower-cased stack across several projects — what a person can evidence."""
    if not project_ids:
        return set()
    return {
        tech.name.strip().lower()
        for _, tech in db.query(ProjectTechnology, Technology)
        .join(Technology, Technology.id == ProjectTechnology.technology_id)
        .filter(ProjectTechnology.project_id.in_(project_ids))
        .all()
        if tech and tech.name
    }
