"""/vendors — section 6 (vendor master) and section 28 (vendor history)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.person import Person
from app.models.resume_version import ResumeVersion
from app.models.submission import Submission
from app.models.user import User
from app.models.vendor import Vendor
from app.repositories.base_repository import apply_updates, ensure_unique, get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.masters import VendorCreate, VendorResponse, VendorUpdate
from app.schemas.sessions import SubmissionResponse
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


@router.get("", response_model=Page[VendorResponse])
def list_vendors(
    search: str | None = Query(None),
    include_inactive: bool = Query(False),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[VendorResponse]:
    query = db.query(Vendor)
    if not include_inactive:
        query = query.filter(Vendor.is_active.is_(True))
    if search:
        query = query.filter(Vendor.name.ilike(f"%{search}%"))
    items, total = list_and_count(query.order_by(Vendor.name), page.skip, page.limit)
    return Page(
        items=[VendorResponse.model_validate(i) for i in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
def create_vendor(
    payload: VendorCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Vendor:
    ensure_unique(db, Vendor, "name", payload.name)
    vendor = Vendor(**payload.model_dump())
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get("/{vendor_id}", response_model=VendorResponse)
def get_vendor(vendor_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> Vendor:
    return get_or_404(db, Vendor, vendor_id, "Vendor")


@router.patch("/{vendor_id}", response_model=VendorResponse)
def update_vendor(
    vendor_id: UUID, payload: VendorUpdate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Vendor:
    vendor = get_or_404(db, Vendor, vendor_id, "Vendor")
    if payload.name and payload.name != vendor.name:
        ensure_unique(db, Vendor, "name", payload.name, exclude_id=vendor_id)
    apply_updates(vendor, payload)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get("/{vendor_id}/history", response_model=list[SubmissionResponse])
def vendor_history(
    vendor_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[SubmissionResponse]:
    """Section 28 — every resume generated for this vendor, newest first."""
    get_or_404(db, Vendor, vendor_id, "Vendor")
    rows = (
        db.query(Submission, Person, ResumeVersion)
        .join(Person, Person.id == Submission.person_id)
        .join(ResumeVersion, ResumeVersion.id == Submission.version_id)
        .filter(Submission.vendor_id == vendor_id)
        .order_by(Submission.created_at.desc())
        .all()
    )
    vendor = get_or_404(db, Vendor, vendor_id, "Vendor")
    return [
        SubmissionResponse(
            **{
                **SubmissionResponse.model_validate(submission).model_dump(),
                "person_name": person.full_name,
                "vendor_name": vendor.name,
                "version_no": version.version_no,
            }
        )
        for submission, person, version in rows
    ]
