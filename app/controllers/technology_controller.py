"""/technologies — sections 12/13 (Technology master, person-specific records)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.technology import Technology
from app.models.user import User
from app.repositories.base_repository import apply_updates, ensure_unique, get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.masters import TechnologyCreate, TechnologyResponse, TechnologyUpdate
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


@router.get("", response_model=Page[TechnologyResponse])
def list_technologies(
    search: str | None = Query(None),
    category: str | None = Query(None),
    include_inactive: bool = Query(False),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[TechnologyResponse]:
    query = db.query(Technology)
    if not include_inactive:
        query = query.filter(Technology.is_active.is_(True))
    if search:
        query = query.filter(Technology.name.ilike(f"%{search}%"))
    if category:
        query = query.filter(Technology.category == category)
    items, total = list_and_count(query.order_by(Technology.name), page.skip, page.limit)
    return Page(
        items=[TechnologyResponse.model_validate(i) for i in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=TechnologyResponse, status_code=status.HTTP_201_CREATED)
def create_technology(
    payload: TechnologyCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Technology:
    ensure_unique(db, Technology, "name", payload.name)
    technology = Technology(**payload.model_dump())
    db.add(technology)
    db.commit()
    db.refresh(technology)
    return technology


@router.get("/{technology_id}", response_model=TechnologyResponse)
def get_technology(
    technology_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Technology:
    return get_or_404(db, Technology, technology_id, "Technology")


@router.patch("/{technology_id}", response_model=TechnologyResponse)
def update_technology(
    technology_id: UUID,
    payload: TechnologyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Technology:
    technology = get_or_404(db, Technology, technology_id, "Technology")
    if payload.name and payload.name != technology.name:
        ensure_unique(db, Technology, "name", payload.name, exclude_id=technology_id)
    apply_updates(technology, payload)
    db.commit()
    db.refresh(technology)
    return technology
