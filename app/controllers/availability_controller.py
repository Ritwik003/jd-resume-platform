"""/availability and /reservations — sections 7–9, 29–31."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.availability import ProfileReservation
from app.models.requirement import Requirement
from app.models.user import User
from app.repositories.base_repository import get_or_404
from app.schemas.availability import (
    AvailableProfileRow,
    ProfileVendorStatusResponse,
    ReservationCreate,
    ReservationExtendRequest,
    ReservationResponse,
    StatusOverrideRequest,
)
from app.schemas.common import Page, PageMeta
from app.services import availability as availability_service
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


@router.get("/vendors/{vendor_id}/profiles", response_model=Page[AvailableProfileRow])
def list_profiles_for_vendor(
    vendor_id: UUID,
    only_selectable: bool = Query(False),
    search: str | None = Query(None),
    profile_type_id: UUID | None = Query(None),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[AvailableProfileRow]:
    """Section 7 — the candidate table for one vendor.

    Filtering is by vendor interaction, engagement, reservation and availability.
    No score is produced: section 7 states the system does not need to rank
    candidates, and section 42 puts ranking out of MVP scope.
    """
    rows, total = availability_service.list_profiles_for_vendor(
        db,
        vendor_id=vendor_id,
        only_selectable=only_selectable,
        search=search,
        profile_type_id=profile_type_id,
        skip=page.skip,
        limit=page.limit,
    )
    db.commit()
    return Page(items=rows, meta=PageMeta(skip=page.skip, limit=page.limit, total=total))


@router.get("/requirements/{requirement_id}/profiles", response_model=Page[AvailableProfileRow])
def list_profiles_for_requirement(
    requirement_id: UUID,
    only_selectable: bool = Query(True),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[AvailableProfileRow]:
    """Convenience wrapper: the vendor is implied by the requirement.

    The requirement supplies the vendor and nothing else. It used to also
    supply the JD's skills, to mark which of them each candidate had — that
    went with the per-person skill record it was read from.
    """
    requirement = get_or_404(db, Requirement, requirement_id, "Requirement")
    rows, total = availability_service.list_profiles_for_vendor(
        db,
        vendor_id=requirement.vendor_id,
        only_selectable=only_selectable,
        skip=page.skip,
        limit=page.limit,
    )
    db.commit()
    return Page(items=rows, meta=PageMeta(skip=page.skip, limit=page.limit, total=total))


@router.get("/persons/{person_id}/vendors/{vendor_id}", response_model=ProfileVendorStatusResponse)
def get_status(
    person_id: UUID, vendor_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ProfileVendorStatusResponse:
    availability_service.expire_stale_reservations(db, vendor_id=vendor_id)
    row = availability_service.get_or_create_status(db, person_id, vendor_id)
    db.commit()
    return ProfileVendorStatusResponse.model_validate(row)


@router.put("/persons/{person_id}/vendors/{vendor_id}", response_model=ProfileVendorStatusResponse)
def override_status(
    person_id: UUID,
    vendor_id: UUID,
    payload: StatusOverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProfileVendorStatusResponse:
    """Section 30 — the manual override the document allows for."""
    row = availability_service.set_status(
        db,
        person_id=person_id,
        vendor_id=vendor_id,
        status=payload.status,
        engagement_label=payload.current_engagement_label,
        notes=payload.notes,
        actor_id=current_user.id,
    )
    db.commit()
    return ProfileVendorStatusResponse.model_validate(row)


# --------------------------------------------------------------- reservations
reservations_router = APIRouter()


@reservations_router.post("", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED)
def create_reservation(
    payload: ReservationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReservationResponse:
    """Section 9 — AVAILABLE -> RESERVED with a configurable expiry."""
    reservation = availability_service.create_reservation(
        db,
        person_id=payload.person_id,
        vendor_id=payload.vendor_id,
        requirement_id=payload.requirement_id,
        duration_minutes=payload.duration_minutes,
        actor_id=current_user.id,
    )
    db.commit()
    return ReservationResponse.model_validate(reservation)


@reservations_router.get("/{reservation_id}", response_model=ReservationResponse)
def get_reservation(
    reservation_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ReservationResponse:
    return ReservationResponse.model_validate(
        get_or_404(db, ProfileReservation, reservation_id, "Reservation")
    )


@reservations_router.post("/{reservation_id}/extend", response_model=ReservationResponse)
def extend_reservation(
    reservation_id: UUID,
    payload: ReservationExtendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReservationResponse:
    """Keeps a mid-edit session alive instead of losing the user's work."""
    reservation = availability_service.extend_reservation(
        db,
        reservation_id=reservation_id,
        additional_minutes=payload.additional_minutes,
        actor_id=current_user.id,
    )
    db.commit()
    return ReservationResponse.model_validate(reservation)


@reservations_router.delete("/{reservation_id}", response_model=ReservationResponse)
def release_reservation(
    reservation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReservationResponse:
    reservation = availability_service.release_reservation(
        db, reservation_id=reservation_id, reason="MANUAL", actor_id=current_user.id
    )
    db.commit()
    return ReservationResponse.model_validate(reservation)


@reservations_router.post("/sweep-expired")
def sweep_expired(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    """Section 31 — manual trigger for the same sweep the background worker runs."""
    count = availability_service.expire_stale_reservations(db)
    db.commit()
    return {"expired": count}
