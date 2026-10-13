"""/search — section 34 (structured search across the knowledge base).

Semantic search is listed as "where useful"; this MVP implements the structured
filtering the examples in section 34 actually need, and leaves embeddings out
(section 42 keeps scope tight).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.person import Person, PersonProfile, ProfileType
from app.models.project import Project, ProjectTechnology
from app.models.technology import PersonTechnology, Technology
from app.models.user import User

router = APIRouter()


@router.get("/profiles")
def search_profiles(
    technologies: list[str] = Query(default=[], description="e.g. Python, FastAPI — matches ALL"),
    profile_type: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    """"Find all AI profiles" / "Find profiles using Python + FastAPI"."""
    # No years filter: a career total is not stored any more. What a person did
    # is on their employment and project rows, and the technology filter below
    # is the question this endpoint is actually asked.
    query = db.query(Person).filter(Person.is_active.is_(True))
    if profile_type:
        query = (
            query.join(PersonProfile, PersonProfile.person_id == Person.id)
            .join(ProfileType, ProfileType.id == PersonProfile.profile_type_id)
            .filter(ProfileType.name.ilike(f"%{profile_type}%"))
        )

    persons = query.all()
    if technologies:
        wanted = {t.strip().lower() for t in technologies if t.strip()}
        filtered = []
        for person in persons:
            owned = {
                tech.name.lower()
                for _, tech in db.query(PersonTechnology, Technology)
                .join(Technology, Technology.id == PersonTechnology.technology_id)
                .filter(PersonTechnology.person_id == person.id)
                .all()
            }
            if wanted <= owned:
                filtered.append(person)
        persons = filtered

    return [
        {
            "person_id": str(p.id),
            "full_name": p.full_name,
            "designation": p.current_designation,
        }
        for p in persons
    ]


@router.get("/projects")
def search_projects(
    technology: str | None = Query(None, description="e.g. RAG"),
    keyword: str | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    """"Find projects involving RAG"."""
    query = db.query(Project).filter(Project.is_active.is_(True))
    if technology:
        query = (
            query.join(ProjectTechnology, ProjectTechnology.project_id == Project.id)
            .join(Technology, Technology.id == ProjectTechnology.technology_id)
            .filter(Technology.name.ilike(f"%{technology}%"))
        )
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(Project.name.ilike(like) | Project.short_description.ilike(like))

    return [
        {
            "project_id": str(p.id),
            "name": p.name,
            "role": p.role,
            "short_description": p.short_description,
        }
        for p in query.limit(100).all()
    ]
