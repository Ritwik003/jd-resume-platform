"""/projects — section 15 (reusable project pool).

A project is a standalone capability record: no person, no company, no dates
of its own. Every one is matched to a JD independently, by name/role/content.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.project import Project, ProjectTechnology
from app.models.technology import Technology
from app.models.user import User
from app.repositories.base_repository import apply_updates, get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.profiles import ProjectCreate, ProjectResponse, ProjectUpdate
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


def _to_response(db: Session, project: Project) -> ProjectResponse:
    tech_names = [
        tech.name
        for _, tech in db.query(ProjectTechnology, Technology)
        .join(Technology, Technology.id == ProjectTechnology.technology_id)
        .filter(ProjectTechnology.project_id == project.id)
        .all()
    ]
    return ProjectResponse(
        **{
            **ProjectResponse.model_validate(project).model_dump(),
            "technology_names": tech_names,
        }
    )


@router.get("", response_model=Page[ProjectResponse])
def list_projects(
    technology_id: UUID | None = Query(None),
    search: str | None = Query(None),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[ProjectResponse]:
    """Section 34 — "Find projects involving RAG"."""
    query = db.query(Project).filter(Project.is_active.is_(True))
    if technology_id:
        query = query.join(ProjectTechnology, ProjectTechnology.project_id == Project.id).filter(
            ProjectTechnology.technology_id == technology_id
        )
    if search:
        like = f"%{search}%"
        query = query.filter(Project.name.ilike(like) | Project.short_description.ilike(like))

    items, total = list_and_count(query.order_by(Project.name), page.skip, page.limit)
    return Page(
        items=[_to_response(db, p) for p in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ProjectResponse:
    data = payload.model_dump(exclude={"technologies"})
    project = Project(**data)
    db.add(project)
    db.flush()

    for link in payload.technologies:
        get_or_404(db, Technology, link.technology_id, "Technology")
        db.add(
            ProjectTechnology(
                project_id=project.id,
                technology_id=link.technology_id,
                is_core="true" if link.is_core else "false",
            )
        )

    db.commit()
    db.refresh(project)
    return _to_response(db, project)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> ProjectResponse:
    return _to_response(db, get_or_404(db, Project, project_id, "Project"))


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: UUID, payload: ProjectUpdate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ProjectResponse:
    project = get_or_404(db, Project, project_id, "Project")
    if payload.technologies is not None:
        db.query(ProjectTechnology).filter(ProjectTechnology.project_id == project_id).delete()
        for link in payload.technologies:
            db.add(
                ProjectTechnology(
                    project_id=project_id,
                    technology_id=link.technology_id,
                    is_core="true" if link.is_core else "false",
                )
            )
    apply_updates(project, payload, exclude={"technologies"})
    db.commit()
    db.refresh(project)
    return _to_response(db, project)
