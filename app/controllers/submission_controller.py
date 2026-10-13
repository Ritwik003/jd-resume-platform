"""/submissions — sections 28/33."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.person import Person
from app.models.resume_version import ResumeVersion
from app.models.submission import Submission
from app.models.user import User
from app.models.vendor import Vendor
from app.repositories.base_repository import get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.sessions import SubmissionResponse, SubmissionStatusUpdate
from app.services import resume_session_service as svc
from app.utils.enums import SubmissionStatus
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


def _to_response(db: Session, submission: Submission) -> SubmissionResponse:
    person = db.query(Person).filter(Person.id == submission.person_id).first()
    vendor = db.query(Vendor).filter(Vendor.id == submission.vendor_id).first()
    version = db.query(ResumeVersion).filter(ResumeVersion.id == submission.version_id).first()
    return SubmissionResponse(
        **{
            **SubmissionResponse.model_validate(submission).model_dump(),
            "person_name": person.full_name if person else None,
            "vendor_name": vendor.name if vendor else None,
            "version_no": version.version_no if version else None,
        }
    )


@router.get("", response_model=Page[SubmissionResponse])
def list_submissions(
    vendor_id: UUID | None = Query(None),
    person_id: UUID | None = Query(None),
    status_filter: SubmissionStatus | None = Query(None, alias="status"),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[SubmissionResponse]:
    query = db.query(Submission)
    if vendor_id:
        query = query.filter(Submission.vendor_id == vendor_id)
    if person_id:
        query = query.filter(Submission.person_id == person_id)
    if status_filter:
        query = query.filter(Submission.status == status_filter.value)
    items, total = list_and_count(query.order_by(Submission.created_at.desc()), page.skip, page.limit)
    return Page(
        items=[_to_response(db, s) for s in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.get("/{submission_id}", response_model=SubmissionResponse)
def get_submission(submission_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return _to_response(db, get_or_404(db, Submission, submission_id, "Submission"))


@router.post("/{submission_id}/status", response_model=SubmissionResponse)
def update_status(
    submission_id: UUID,
    payload: SubmissionStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SubmissionResponse:
    """Section 33 — the status drives the profile's vendor availability.

    SUBMITTED marks the person ENGAGED for that vendor only; REJECTED returns
    them to that same vendor's pool with no manual restoration (section 30).
    """
    submission = get_or_404(db, Submission, submission_id, "Submission")
    svc.set_submission_status(db, submission, payload.status, reason=payload.reason, actor_id=user.id)
    db.commit()
    return _to_response(db, submission)
