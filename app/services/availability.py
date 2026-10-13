"""Sections 7–9, 29–31 — vendor-scoped availability and the reservation lifecycle.

Availability is keyed on (person, vendor). The same person may be AVAILABLE to
one vendor and ENGAGED with another, so every read and write in this module is
scoped by vendor.

One hold, two phases. Selecting a person opens a ``ProfileReservation`` and puts
the pair in EDITING; approving the resume keeps that same reservation row and
moves the pair to RESERVED with a clock. Keeping one lock object across both
phases is what makes the sweeper, the "can I still edit this?" check and the
tracking view agree with each other — two lock objects would eventually
disagree, and the profile would be held by one and free by the other.

Only RESERVED carries a clock. EDITING's ``expires_at`` is NULL — an open tab
or a composition left overnight is not a reason to let the person go — so it
is held until somebody approves it (RESERVED, on a clock) or explicitly
releases it. Reopening an approved resume for more edits (``reopen``) drops it
straight back to that same clock-free EDITING hold.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import RESERVATION_DURATION_MINUTES
from app.models.availability import ProfileReservation, ProfileVendorStatus
from app.models.person import Person, PersonProfile
from app.models.requirement import Requirement
from app.models.resume_session import ResumeCompositionSession
from app.models.vendor import Vendor
from app.schemas.availability import AvailableProfileRow
from app.services import audit
from app.utils.enums import MANUAL_TRANSITIONS, ProfileVendorStatusEnum, ResumeSessionStatus
from app.utils.exceptions import (
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    ProfileNotAvailableError,
)
from app.utils.helper import as_aware, utcnow

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- status
def get_or_create_status(db: Session, person_id: UUID, vendor_id: UUID) -> ProfileVendorStatus:
    """Every (person, vendor) pair defaults to AVAILABLE until stated otherwise.

    Written to survive a race rather than assume there is none. ``(person_id,
    vendor_id)`` carries a unique constraint (``uq_profile_vendor``), and the
    old "check, then insert" version raced on it: two requests for a pair
    that has no row yet both see nothing, both try to INSERT, and the second
    blocks on the first's row lock until it commits or rolls back — for
    however long that takes. Seen in practice as a single-row insert timing
    out at the database's own statement_timeout with nothing slow enough on
    its own to explain it: the wait was for another transaction (in this
    codebase's own history, a duplicate server process left running after a
    restart), not for the disk.

    ``begin_nested`` (a SAVEPOINT) is what makes this portable: Postgres's
    ``INSERT ... ON CONFLICT`` needs its own row to conflict against and does
    not exist in SQLite, which this test suite runs on, so the fix has to be
    "try the insert, and if the unique constraint says somebody beat us to
    it, read what they wrote" — the same logic under either database. It does
    not remove the lock wait on Postgres when two inserts for the same new
    pair are genuinely simultaneous (nothing can, short of external locking);
    it removes the failure mode where that wait's outcome was an unhandled
    500 instead of both callers ending up with the same row.
    """
    row = (
        db.query(ProfileVendorStatus)
        .filter(ProfileVendorStatus.person_id == person_id, ProfileVendorStatus.vendor_id == vendor_id)
        .first()
    )
    if row is not None:
        return row

    try:
        with db.begin_nested():
            row = ProfileVendorStatus(
                person_id=person_id, vendor_id=vendor_id, status=ProfileVendorStatusEnum.AVAILABLE
            )
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # Lost the race: someone else's row for this pair committed between
        # our SELECT and our INSERT. Read theirs rather than erroring.
        row = (
            db.query(ProfileVendorStatus)
            .filter(ProfileVendorStatus.person_id == person_id, ProfileVendorStatus.vendor_id == vendor_id)
            .first()
        )
        if row is None:  # pragma: no cover - would mean the constraint itself is gone
            raise
        return row


def _require_predecessor(status_row: ProfileVendorStatus, target: ProfileVendorStatusEnum) -> None:
    """Refuse a hand-set status that does not follow from where the pair stands.

    ``MANUAL_TRANSITIONS`` is the one dependency chain both callers of these
    marks — the vendor tracking "Set status" action and the submission status
    endpoint — go through, so a decision can never be typed in out of order
    (e.g. RELEASED without ever having been ENGAGED) no matter which screen it
    came from.
    """
    current = ProfileVendorStatusEnum(status_row.status)
    allowed = MANUAL_TRANSITIONS.get(target, ())
    if current not in allowed:
        allowed_msg = " or ".join(str(s) for s in allowed) if allowed else "no state"
        raise InvalidTransitionError(
            f"Cannot mark {target} for this vendor — the profile is {current}, "
            f"and {target} only follows from {allowed_msg}.",
            details={"status": str(current), "target": str(target)},
        )


#: The one state a clock runs against. RESERVED is an approved resume nobody
#: answered, and it lapses so a profile is never shelved forever by a
#: submission everybody forgot. EDITING carries no expiry at all — closing the
#: tab does not release the person — so it never appears here or in the sweep.
_TIMED_HOLDS = (ProfileVendorStatusEnum.RESERVED,)


def expire_stale_reservations(db: Session, *, vendor_id: UUID | None = None) -> int:
    """Section 31 — a hold past its expiry lets the profile go.

    Called by the background sweeper and, defensively, before any availability
    read so a stale lock can never hide a profile from the candidate list.

    The pair returns to AVAILABLE and the session it belonged to goes back to
    EDITING. Nothing is destroyed: the composition still exists, and editing it
    takes a fresh hold (see ``resume_session_service.revive_session``).
    """
    now = utcnow()
    query = db.query(ProfileVendorStatus).filter(
        ProfileVendorStatus.status.in_([str(s) for s in _TIMED_HOLDS]),
        ProfileVendorStatus.reservation_expires_at.isnot(None),
    )
    if vendor_id is not None:
        query = query.filter(ProfileVendorStatus.vendor_id == vendor_id)

    expired = [row for row in query.all() if (as_aware(row.reservation_expires_at) or now) <= now]
    for row in expired:
        row.status = ProfileVendorStatusEnum.AVAILABLE
        row.last_interaction_at = now
        row.reserved_at = None
        row.reservation_expires_at = None
        row.current_requirement_id = None

    # The session the hold belonged to says so too, or History shows a session
    # sitting in RESERVED while the vendor view says the hold is gone. Only the
    # unsettled ones move: a session already SUBMITTED, ENGAGED, RELEASED or
    # REJECTED records a decision somebody made, and a clock does not undo that.
    for row in expired:
        for session in db.query(ResumeCompositionSession).filter(
            ResumeCompositionSession.person_id == row.person_id,
            ResumeCompositionSession.vendor_id == row.vendor_id,
            ResumeCompositionSession.status.in_(
                [str(ResumeSessionStatus.EDITING), str(ResumeSessionStatus.RESERVED)]
            ),
        ):
            session.status = ResumeSessionStatus.EDITING

    if expired:
        person_ids = [row.person_id for row in expired]
        vendor_ids = [row.vendor_id for row in expired]
        reservations = (
            db.query(ProfileReservation)
            .filter(
                ProfileReservation.released_at.is_(None),
                ProfileReservation.person_id.in_(person_ids),
                ProfileReservation.vendor_id.in_(vendor_ids),
            )
            .all()
        )
        for res in reservations:
            if (as_aware(res.expires_at) or now) <= now:
                res.released_at = now
                res.release_reason = "EXPIRED"
        db.flush()
        logger.info("Expired %s stale reservation(s)", len(expired))
    return len(expired)


# ----------------------------------------------------------- candidate listing
def list_profiles_for_vendor(
    db: Session,
    *,
    vendor_id: UUID,
    only_selectable: bool = False,
    search: str | None = None,
    profile_type_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[AvailableProfileRow], int]:
    """Section 7 — the candidate table: who is free for this vendor.

    That is the whole question. Filtering is by vendor interaction status,
    engagement and reservation, and the answer is never scored or ordered by
    fitness — section 7 says the system does not need to rank candidates and
    section 42 puts ranking out of MVP scope.

    No role and no skills come back with a row. A person holds neither: the role
    is whatever the JD asks for, applied when they are selected, and what a
    resume claims is composed for that JD out of the shared bank rather than
    read off the candidate. See ``AvailableProfileRow`` for why showing either
    would be worse than showing nothing.
    """
    expire_stale_reservations(db, vendor_id=vendor_id)

    if db.query(Vendor).filter(Vendor.id == vendor_id).first() is None:
        raise NotFoundError("Vendor not found")

    person_query = db.query(Person).filter(Person.is_active.is_(True))
    if search:
        person_query = person_query.filter(Person.full_name.ilike(f"%{search.strip()}%"))

    if profile_type_id is not None:
        # Narrows to people already put forward under this role at some point.
        # Nothing in the UI asks for this; it is kept for callers that want to
        # start from a role rather than from a vendor.
        person_query = person_query.join(PersonProfile, PersonProfile.person_id == Person.id).filter(
            PersonProfile.profile_type_id == profile_type_id,
            PersonProfile.is_active.is_(True),
        )

    persons = person_query.order_by(Person.full_name).all()
    person_ids = [p.id for p in persons]
    if not person_ids:
        return [], 0

    status_map = {
        row.person_id: row
        for row in db.query(ProfileVendorStatus).filter(
            ProfileVendorStatus.vendor_id == vendor_id,
            ProfileVendorStatus.person_id.in_(person_ids),
        )
    }

    # Where each person is actually working, whoever it is for — read across
    # every vendor, not just the one being listed.
    #
    # This is the one column on the table that is deliberately NOT scoped to
    # this vendor. Availability is per (person, vendor), so somebody ENGAGED
    # with Apex is still legitimately selectable for Kestrel (§8) and their
    # Kestrel row says AVAILABLE — correctly. But the engagement lives on the
    # Apex row, so scoping this column to the listed vendor could only ever
    # show a blank, which reads as "free" rather than "free, and currently
    # placed elsewhere". Whoever is choosing needs the second one.
    engagements: dict[UUID, list[str]] = {}
    for engaged_row, engaged_vendor in (
        db.query(ProfileVendorStatus, Vendor)
        .join(Vendor, Vendor.id == ProfileVendorStatus.vendor_id)
        .filter(
            ProfileVendorStatus.person_id.in_(person_ids),
            ProfileVendorStatus.status == str(ProfileVendorStatusEnum.ENGAGED),
        )
        .all()
    ):
        # The free-text note is an optional embellishment ("Client A — GenAI");
        # the vendor's name is the fact, and is always there to fall back on.
        note = engaged_row.current_engagement_label
        engagements.setdefault(engaged_row.person_id, []).append(
            f"{engaged_vendor.name} — {note}" if note else engaged_vendor.name
        )

    rows: list[AvailableProfileRow] = []
    for person in persons:
        status_row = status_map.get(person.id)
        status_value = status_row.status if status_row else ProfileVendorStatusEnum.AVAILABLE
        selectable = ProfileVendorStatusEnum(status_value).is_selectable
        if only_selectable and not selectable:
            continue

        rows.append(
            AvailableProfileRow(
                person_id=person.id,
                person_name=person.full_name,
                current_engagement_label=", ".join(sorted(engagements.get(person.id, []))) or None,
                vendor_status=str(status_value),
                is_selectable=selectable,
                reservation_expires_at=as_aware(status_row.reservation_expires_at) if status_row else None,
            )
        )

    total = len(rows)
    return rows[skip : skip + limit], total


# ---------------------------------------------------------------- reservation
def create_reservation(
    db: Session,
    *,
    person_id: UUID,
    vendor_id: UUID,
    requirement_id: UUID,
    duration_minutes: int | None = None,
    actor_id: UUID | None = None,
) -> ProfileReservation:
    """Section 9 — AVAILABLE -> EDITING, open-ended unless a duration is asked for.

    Selecting somebody starts a composition, not a commitment. The lock is real
    from this moment — nobody else may pick them for another requirement — but
    what exists so far is an unfinished resume, and calling that RESERVED would
    make the tracking view unable to tell it apart from an approved one waiting
    on the vendor's answer.

    No clock runs against it by default: closing the tab is not a reason to let
    the person go, and a composition left for a week is still a composition, not
    an abandoned lock. Passing ``duration_minutes`` still gives a caller a
    time-boxed hold for the cases that want one; the default is unbounded.
    """
    expire_stale_reservations(db, vendor_id=vendor_id)

    if db.query(Person).filter(Person.id == person_id).first() is None:
        raise NotFoundError("Person not found")
    if db.query(Requirement).filter(Requirement.id == requirement_id).first() is None:
        raise NotFoundError("Requirement not found")

    status_row = get_or_create_status(db, person_id, vendor_id)
    if not ProfileVendorStatusEnum(status_row.status).is_selectable:
        raise ProfileNotAvailableError(
            f"Profile is {status_row.status} for this vendor and cannot be reserved",
            details={"status": str(status_row.status)},
        )

    now = utcnow()
    expires_at = now + timedelta(minutes=duration_minutes) if duration_minutes else None

    reservation = ProfileReservation(
        person_id=person_id,
        vendor_id=vendor_id,
        requirement_id=requirement_id,
        reserved_at=now,
        expires_at=expires_at,
        reserved_by_id=actor_id,
    )
    db.add(reservation)

    status_row.status = ProfileVendorStatusEnum.EDITING
    status_row.reserved_at = now
    status_row.reservation_expires_at = expires_at
    status_row.last_interaction_at = now
    status_row.current_requirement_id = requirement_id

    audit.record(
        db,
        action="PROFILE_EDITING",
        entity_type="ProfileReservation",
        entity_id=reservation.id,
        actor_id=actor_id,
        summary=f"Reserved for {duration_minutes} minutes" if duration_minutes else "Reserved with no time limit",
        payload={"person_id": str(person_id), "vendor_id": str(vendor_id), "requirement_id": str(requirement_id)},
    )
    db.flush()
    return reservation


def extend_reservation(
    db: Session, *, reservation_id: UUID, additional_minutes: int = 60, actor_id: UUID | None = None
) -> ProfileReservation:
    """Let a user who is mid-edit keep the lock rather than losing their session."""
    reservation = db.query(ProfileReservation).filter(ProfileReservation.id == reservation_id).first()
    if reservation is None:
        raise NotFoundError("Reservation not found")
    if reservation.released_at is not None:
        raise ConflictError("Reservation has already been released")

    base = max(as_aware(reservation.expires_at) or utcnow(), utcnow())
    reservation.expires_at = base + timedelta(minutes=additional_minutes)

    status_row = get_or_create_status(db, reservation.person_id, reservation.vendor_id)
    # Extending puts time back on the clock; it does not change what phase the
    # hold is in. Forcing RESERVED here would silently promote an unfinished
    # composition, and forcing EDITING would demote an approved resume.
    if status_row.status not in (ProfileVendorStatusEnum.EDITING, ProfileVendorStatusEnum.RESERVED):
        status_row.status = ProfileVendorStatusEnum.EDITING
    status_row.reserved_at = status_row.reserved_at or utcnow()
    status_row.reservation_expires_at = reservation.expires_at

    audit.record(
        db,
        action="RESERVATION_EXTENDED",
        entity_type="ProfileReservation",
        entity_id=reservation.id,
        actor_id=actor_id,
        summary=f"Extended by {additional_minutes} minutes",
    )
    db.flush()
    return reservation


def release_reservation(
    db: Session, *, reservation_id: UUID, reason: str = "MANUAL", actor_id: UUID | None = None
) -> ProfileReservation:
    reservation = db.query(ProfileReservation).filter(ProfileReservation.id == reservation_id).first()
    if reservation is None:
        raise NotFoundError("Reservation not found")
    if reservation.released_at is not None:
        return reservation

    reservation.released_at = utcnow()
    reservation.release_reason = reason

    status_row = get_or_create_status(db, reservation.person_id, reservation.vendor_id)
    if status_row.status in (ProfileVendorStatusEnum.EDITING, ProfileVendorStatusEnum.RESERVED):
        status_row.status = ProfileVendorStatusEnum.AVAILABLE
        status_row.reserved_at = None
        status_row.reservation_expires_at = None
        status_row.current_requirement_id = None

    audit.record(
        db,
        action="RESERVATION_RELEASED",
        entity_type="ProfileReservation",
        entity_id=reservation.id,
        actor_id=actor_id,
        summary=reason,
    )
    db.flush()
    return reservation


def active_reservation_for(db: Session, *, person_id: UUID, vendor_id: UUID) -> ProfileReservation | None:
    """The reservation currently holding this pair, if any.

    ``expires_at is None`` is an open-ended EDITING hold — no clock, so it is
    active until released rather than until some time passes.
    """
    now = utcnow()
    reservations = (
        db.query(ProfileReservation)
        .filter(
            ProfileReservation.person_id == person_id,
            ProfileReservation.vendor_id == vendor_id,
            ProfileReservation.released_at.is_(None),
        )
        .order_by(ProfileReservation.reserved_at.desc())
        .all()
    )
    for reservation in reservations:
        if reservation.expires_at is None or as_aware(reservation.expires_at) > now:
            return reservation
    return None


# ------------------------------------------------------------------ transitions
def mark_reserved(
    db: Session,
    *,
    person_id: UUID,
    vendor_id: UUID,
    requirement_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> ProfileVendorStatus:
    """EDITING -> RESERVED: an approved resume is now waiting on this vendor.

    Approval ends the composition and starts a different kind of wait. The
    profile stays locked — it is committed to this vendor — but nobody has
    agreed to anything yet, so the hold carries a clock: if the vendor never
    answers, ``expire_stale_reservations`` lets the person go rather than
    leaving them shelved indefinitely by a submission everybody forgot.

    The clock resets to the same base duration as the edit lock
    (``RESERVATION_DURATION_MINUTES``) rather than a separate, longer default —
    a vendor still reading gets more time the honest way, through
    ``extend_reservation``, instead of a hold that outlasts anyone's attention.

    The studio's own reservation row is reused and extended rather than closed
    and reopened, so there is exactly one thing holding this profile and one
    expiry to reason about.
    """
    status_row = get_or_create_status(db, person_id, vendor_id)
    now = utcnow()
    expires_at = now + timedelta(minutes=RESERVATION_DURATION_MINUTES)

    status_row.status = ProfileVendorStatusEnum.RESERVED
    status_row.reserved_at = now
    status_row.reservation_expires_at = expires_at
    status_row.last_interaction_at = now
    status_row.engagement_end = None
    if requirement_id:
        status_row.current_requirement_id = requirement_id

    reservation = active_reservation_for(db, person_id=person_id, vendor_id=vendor_id)
    if reservation is not None:
        reservation.expires_at = expires_at
    else:
        # The studio hold had already lapsed by the time approval landed. The
        # approval is still real, so a fresh row records the hold it creates.
        reservation = ProfileReservation(
            person_id=person_id,
            vendor_id=vendor_id,
            requirement_id=requirement_id,
            reserved_at=now,
            expires_at=expires_at,
            reserved_by_id=actor_id,
        )
        db.add(reservation)

    audit.record(
        db,
        action="PROFILE_RESERVED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
        summary=f"Held for this vendor for {RESERVATION_DURATION_MINUTES} minutes pending their answer",
        payload={"person_id": str(person_id), "vendor_id": str(vendor_id)},
    )
    db.flush()
    return status_row


def mark_submitted(
    db: Session,
    *,
    person_id: UUID,
    vendor_id: UUID,
    requirement_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> ProfileVendorStatus:
    """RESERVED -> SUBMITTED: the resume has gone to the vendor.

    Still a hold, and deliberately without a clock: once a document is with a
    vendor, letting the profile go on a timer would offer the same person to
    somebody else while the first vendor is reading. It leaves this state only
    when a human says what happened.
    """
    status_row = get_or_create_status(db, person_id, vendor_id)
    _require_predecessor(status_row, ProfileVendorStatusEnum.SUBMITTED)
    now = utcnow()
    status_row.status = ProfileVendorStatusEnum.SUBMITTED
    status_row.last_interaction_at = now
    status_row.reserved_at = None
    status_row.reservation_expires_at = None
    if requirement_id:
        status_row.current_requirement_id = requirement_id

    reservation = active_reservation_for(db, person_id=person_id, vendor_id=vendor_id)
    if reservation is not None:
        reservation.released_at = now
        reservation.release_reason = "CONVERTED_TO_SUBMISSION"

    audit.record(
        db,
        action="PROFILE_SUBMITTED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
        payload={"person_id": str(person_id), "vendor_id": str(vendor_id)},
    )
    db.flush()
    return status_row


def mark_engaged(
    db: Session, *, person_id: UUID, vendor_id: UUID, requirement_id: UUID | None = None, actor_id: UUID | None = None
) -> ProfileVendorStatus:
    """Section 13 of the worked example — submitted profiles leave that vendor's pool."""
    status_row = get_or_create_status(db, person_id, vendor_id)
    _require_predecessor(status_row, ProfileVendorStatusEnum.ENGAGED)
    now = utcnow()
    status_row.status = ProfileVendorStatusEnum.ENGAGED
    status_row.engagement_start = now
    status_row.engagement_end = None
    status_row.last_interaction_at = now
    status_row.reserved_at = None
    status_row.reservation_expires_at = None
    if requirement_id:
        status_row.current_requirement_id = requirement_id

    reservation = active_reservation_for(db, person_id=person_id, vendor_id=vendor_id)
    if reservation is not None:
        reservation.released_at = now
        reservation.release_reason = "CONVERTED_TO_ENGAGEMENT"

    audit.record(
        db,
        action="PROFILE_ENGAGED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
        payload={"person_id": str(person_id), "vendor_id": str(vendor_id)},
    )
    db.flush()
    return status_row


def mark_rejected(
    db: Session, *, person_id: UUID, vendor_id: UUID, actor_id: UUID | None = None, note: str | None = None
) -> ProfileVendorStatus:
    """Section 30 — rejection returns the profile to that same vendor's pool.

    The stored status is REJECTED (so the history stays visible in the candidate
    list) and ``is_selectable`` treats REJECTED as choosable again, which is what
    "no manual restoration should be required" means in practice.
    """
    status_row = get_or_create_status(db, person_id, vendor_id)
    _require_predecessor(status_row, ProfileVendorStatusEnum.REJECTED)
    now = utcnow()
    status_row.status = ProfileVendorStatusEnum.REJECTED
    status_row.engagement_end = now
    status_row.last_interaction_at = now
    status_row.current_requirement_id = None
    status_row.reserved_at = None
    status_row.reservation_expires_at = None
    if note:
        status_row.notes = note

    audit.record(
        db,
        action="PROFILE_REJECTED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
        summary=note,
    )
    db.flush()
    return status_row


def mark_released(
    db: Session, *, person_id: UUID, vendor_id: UUID, actor_id: UUID | None = None
) -> ProfileVendorStatus:
    """ENGAGED -> RELEASED: the engagement with this vendor is over.

    Reachable only from ENGAGED — you can only let go of somebody who was
    actually taken — and, per ``is_selectable``, it is a dead end: the profile
    never becomes selectable for this vendor again.
    """
    status_row = get_or_create_status(db, person_id, vendor_id)
    _require_predecessor(status_row, ProfileVendorStatusEnum.RELEASED)
    now = utcnow()
    status_row.status = ProfileVendorStatusEnum.RELEASED
    status_row.engagement_end = now
    status_row.last_interaction_at = now
    status_row.current_requirement_id = None
    status_row.reserved_at = None
    status_row.reservation_expires_at = None

    audit.record(
        db,
        action="PROFILE_RELEASED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
    )
    db.flush()
    return status_row


def set_status(
    db: Session,
    *,
    person_id: UUID,
    vendor_id: UUID,
    status: ProfileVendorStatusEnum,
    engagement_label: str | None = None,
    notes: str | None = None,
    actor_id: UUID | None = None,
) -> ProfileVendorStatus:
    """Manual override (section 30 — "unless the organization wants a manual override").

    Only the four statuses in ``MANUALLY_SETTABLE`` are decisions a human makes
    directly; AVAILABLE, EDITING and RESERVED are consequences the system
    reaches on its own (selecting, approving, or a hold's clock running out),
    so there is deliberately no branch here that can set them by hand.
    """
    if status == ProfileVendorStatusEnum.SUBMITTED:
        row = mark_submitted(db, person_id=person_id, vendor_id=vendor_id, actor_id=actor_id)
    elif status == ProfileVendorStatusEnum.ENGAGED:
        row = mark_engaged(db, person_id=person_id, vendor_id=vendor_id, actor_id=actor_id)
    elif status == ProfileVendorStatusEnum.REJECTED:
        row = mark_rejected(db, person_id=person_id, vendor_id=vendor_id, actor_id=actor_id, note=notes)
    elif status == ProfileVendorStatusEnum.RELEASED:
        row = mark_released(db, person_id=person_id, vendor_id=vendor_id, actor_id=actor_id)
    else:
        raise InvalidTransitionError(
            f"{status} cannot be set by hand.",
            details={"target": str(status)},
        )
    if engagement_label is not None:
        row.current_engagement_label = engagement_label
    if notes is not None:
        row.notes = notes
    db.flush()
    return row


def vendor_history_for_person(db: Session, person_id: UUID) -> list[ProfileVendorStatus]:
    """Section 28/29 — the per-vendor status of one person, across all vendors."""
    return (
        db.query(ProfileVendorStatus)
        .filter(ProfileVendorStatus.person_id == person_id)
        .order_by(ProfileVendorStatus.updated_at.desc())
        .all()
    )
