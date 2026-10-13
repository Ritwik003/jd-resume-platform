"""Section 29 — vendor tracking.

Three endpoints, one question: for a given vendor, who is ours to talk about and
where does each one stand. See ``app/services/vendor_tracking.py`` for why this
is not the submissions list with a different filter.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.user import User
from app.schemas.vendor_tracking import (
    ExtendHoldRequest,
    TrackedProfileRow,
    TrackingStatusChange,
    VendorDetail,
    VendorTrackingSummary,
)
from app.services import vendor_tracking
from app.utils.enums import MANUALLY_SETTABLE
from app.utils.exceptions import ValidationError

router = APIRouter()


@router.get("", response_model=list[VendorTrackingSummary])
def list_vendors(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[VendorTrackingSummary]:
    """Every vendor, with a count per state. The landing list of the module."""
    rows = vendor_tracking.summary(db)
    db.commit()
    return rows


@router.get("/profiles", response_model=list[TrackedProfileRow])
def all_tracked_profiles(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[TrackedProfileRow]:
    """Every profile put forward to anybody, most in need of attention first.

    The one view that does not get worse as vendors are added. Picking through
    fifty sidebar entries to find the three holds about to lapse is the failure
    mode this exists to remove: the same rows, sorted by what needs doing, with
    the vendor as a column rather than as a navigation step.
    """
    rows = vendor_tracking.tracked_profiles(db)
    db.commit()
    return rows


@router.get("/{vendor_id}/profiles", response_model=list[TrackedProfileRow])
def profiles_for_vendor(
    vendor_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[TrackedProfileRow]:
    """Every profile put forward to this vendor, and where each one stands."""
    rows = vendor_tracking.tracked_profiles(db, vendor_id)
    db.commit()
    return rows


@router.post("/{vendor_id}/profiles/{person_id}/extend", response_model=list[TrackedProfileRow])
def extend_profile_hold(
    vendor_id: UUID,
    person_id: UUID,
    payload: ExtendHoldRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackedProfileRow]:
    """Add time to a pair's RESERVED clock — the Reservations screen's own control."""
    vendor_tracking.extend_pair_hold(
        db,
        vendor_id=vendor_id,
        person_id=person_id,
        additional_minutes=payload.additional_minutes,
        actor_id=current_user.id,
    )
    rows = vendor_tracking.tracked_profiles(db, vendor_id)
    db.commit()
    return rows


@router.post("/{vendor_id}/profiles/{person_id}/release-hold", response_model=list[TrackedProfileRow])
def release_orphan_hold(
    vendor_id: UUID,
    person_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackedProfileRow]:
    """Let go of a hold with no resume session behind it — see the service docstring.

    The one action available for a row this table otherwise has none for: no
    session means no Open, no Delete, and nothing for Set status to advance,
    so a profile reserved and then abandoned before a composition ever
    started had no way back to AVAILABLE at all.
    """
    vendor_tracking.release_orphan_hold(
        db, vendor_id=vendor_id, person_id=person_id, actor_id=current_user.id
    )
    rows = vendor_tracking.tracked_profiles(db, vendor_id)
    db.commit()
    return rows


@router.get("/{vendor_id}/detail", response_model=VendorDetail)
def vendor_detail(
    vendor_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> VendorDetail:
    """One vendor's own page: everything sent, everyone working, everyone finished.

    Read from the Knowledge base, where a vendor is a record you open rather
    than a filter you apply — the tracking board answers "what needs doing
    across vendors", this answers "what is our history with this one".
    """
    detail = vendor_tracking.vendor_detail(db, vendor_id)
    db.commit()
    return detail


@router.post("/{vendor_id}/profiles/{person_id}/reopen", response_model=list[TrackedProfileRow])
def reopen_released_profile(
    vendor_id: UUID,
    person_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackedProfileRow]:
    """Make a released profile offerable to this vendor again.

    Deliberately its own endpoint rather than a status in the ordinary picker:
    RELEASED is a terminal state on purpose, and reopening it is an exception
    somebody chooses, not the next step in a flow.
    """
    vendor_tracking.reopen_released_pair(
        db, vendor_id=vendor_id, person_id=person_id, actor_id=current_user.id
    )
    rows = vendor_tracking.tracked_profiles(db, vendor_id)
    db.commit()
    return rows


@router.post("/{vendor_id}/profiles/{person_id}/status", response_model=list[TrackedProfileRow])
def set_profile_status(
    vendor_id: UUID,
    person_id: UUID,
    payload: TrackingStatusChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackedProfileRow]:
    """Advance one pair by hand, and return the refreshed list.

    Only the states a human is entitled to assert are accepted. EDITING and
    RESERVED are consequences of composing and approving a resume, and
    AVAILABLE is a consequence of a hold's clock running out — typing any of
    the three in would put the status row at odds with the session and
    submission underneath it, which is exactly the kind of disagreement this
    view exists to surface. The service layer additionally enforces that even
    an allowed status is only accepted from the current status it may legally
    follow (``MANUAL_TRANSITIONS``) — e.g. RELEASED is refused unless the pair
    is currently ENGAGED.

    The whole list comes back rather than the single row, because setting one
    status can change what the rest of the screen should say and a client that
    patches one row locally will drift.
    """
    if payload.status not in MANUALLY_SETTABLE:
        allowed = ", ".join(str(s) for s in MANUALLY_SETTABLE)
        raise ValidationError(
            f"{payload.status} follows from the resume or the clock rather than from a "
            f"decision, so it cannot be set by hand. Settable here: {allowed}."
        )

    # Going through the service rather than straight at the status row: setting
    # a status here has to carry the session and the newest submission with it,
    # or History and this view start telling different stories about the same
    # person. One vocabulary is only worth having if all three actually say it.
    vendor_tracking.set_pair_status(
        db,
        vendor_id=vendor_id,
        person_id=person_id,
        status=payload.status,
        engagement_label=payload.engagement_label,
        notes=payload.notes,
        actor_id=current_user.id,
    )
    rows = vendor_tracking.tracked_profiles(db, vendor_id)
    db.commit()
    return rows
