"""/companies — section 16 (Company pool)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.company import Company
from app.models.user import User
from app.repositories.base_repository import apply_updates, ensure_unique, get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.masters import CompanyCreate, CompanyResponse, CompanyUpdate
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


@router.get("", response_model=Page[CompanyResponse])
def list_companies(
    search: str | None = Query(None),
    include_inactive: bool = Query(False),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[CompanyResponse]:
    query = db.query(Company)
    if not include_inactive:
        query = query.filter(Company.is_active.is_(True))
    if search:
        query = query.filter(Company.name.ilike(f"%{search}%"))
    items, total = list_and_count(query.order_by(Company.name), page.skip, page.limit)
    return Page(
        items=[CompanyResponse.model_validate(i) for i in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
def create_company(
    payload: CompanyCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Company:
    ensure_unique(db, Company, "name", payload.name)
    company = Company(**payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.get("/{company_id}", response_model=CompanyResponse)
def get_company(company_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> Company:
    return get_or_404(db, Company, company_id, "Company")


@router.patch("/{company_id}", response_model=CompanyResponse)
def update_company(
    company_id: UUID,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Company:
    company = get_or_404(db, Company, company_id, "Company")
    if payload.name and payload.name != company.name:
        ensure_unique(db, Company, "name", payload.name, exclude_id=company_id)
    apply_updates(company, payload)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_company(
    company_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> None:
    """Soft delete — master data stays referenced by historical resumes."""
    company = get_or_404(db, Company, company_id, "Company")
    company.is_active = False
    db.commit()
