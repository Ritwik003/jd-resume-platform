"""Section 29 — the vendor's side of the ledger.

Everywhere else in this platform the question is "what can we send?" — you start
from a JD, you pick a person, you build a resume. This module asks the mirror
question: **for this vendor, who is currently ours to talk about?**

That is not a different rendering of the submissions list. A submission is a
document that went out; what a vendor manager needs is the *state of each
person against that vendor* — who is mid-composition, who is sitting on an
approved resume nobody has answered, who is working, and whose hold quietly
lapsed last Tuesday. Those live on ``ProfileVendorStatus``, and four of the
seven states have no submission row at all.

So the unit here is the (person, vendor) pair, and a row is assembled from three
places: the status row for where it stands, the newest composition session for
what it is about, and that session's submission for the paperwork. Assembled
server-side because doing it in the browser is one request per person.

Which pairs count as "selected for this vendor" is deliberately generous: every
pair that has ever had a composition session, plus any pair a human has moved
off AVAILABLE by hand. A bare AVAILABLE row is excluded — those get created as a
side effect of merely *reading* a status, so listing them would fill the view
with people nobody ever put forward.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.availability import ProfileVendorStatus
from app.models.person import Person
from app.models.requirement import Requirement
from app.models.resume_session import ResumeCompositionSession
from app.models.resume_version import ResumeVersion
from app.models.submission import Submission
from app.models.vendor import Vendor
from app.schemas.vendor_tracking import (
    TrackedProfileRow,
    VendorDetail,
    VendorEngagementRow,
    VendorSubmissionRow,
    VendorTrackingSummary,
)
from app.services import audit, availability
from app.utils.enums import ProfileVendorStatusEnum, ResumeSessionStatus, SubmissionStatus
from app.utils.exceptions import ConflictError, NotFoundError
from app.utils.helper import as_aware, utcnow


def _tracked_pairs(db: Session, vendor_id: UUID | None = None) -> set[tuple[UUID, UUID]]:
    """The (vendor, person) pairs that count as "put forward".

    Every pair that has ever had a composition session, plus any pair a human
    has moved off AVAILABLE by hand — MINUS any pair that is AVAILABLE right
    now. Pass ``vendor_id`` for one vendor, or omit it for the cross-vendor
    view — the rule has to be identical in both, or the sidebar counts and the
    table disagree.

    The subtraction is what keeps this view about what needs attention. A hold
    nobody acted on lapses back to AVAILABLE on its own (see
    ``expire_stale_reservations``), and at that moment the person is simply
    offerable to that vendor again — the same as anyone who was never put
    forward. Listing them anyway put a green AVAILABLE row in a table whose
    every other row is something to act on, and implied a claim on the person
    that no longer exists. Their resumes are not lost with the row: the person
    drawer still lists every one that was built, and its vendor-status tab
    still shows where they stand with each vendor.
    """
    sessions = db.query(ResumeCompositionSession.vendor_id, ResumeCompositionSession.person_id).filter(
        ResumeCompositionSession.vendor_id.isnot(None)
    )
    statuses = db.query(ProfileVendorStatus.vendor_id, ProfileVendorStatus.person_id).filter(
        ProfileVendorStatus.status != str(ProfileVendorStatusEnum.AVAILABLE)
    )
    available = db.query(ProfileVendorStatus.vendor_id, ProfileVendorStatus.person_id).filter(
        ProfileVendorStatus.status == str(ProfileVendorStatusEnum.AVAILABLE)
    )
    if vendor_id is not None:
        sessions = sessions.filter(ResumeCompositionSession.vendor_id == vendor_id)
        statuses = statuses.filter(ProfileVendorStatus.vendor_id == vendor_id)
        available = available.filter(ProfileVendorStatus.vendor_id == vendor_id)

    tracked = {tuple(row) for row in sessions.distinct().all()} | {tuple(row) for row in statuses.all()}
    return tracked - {tuple(row) for row in available.all()}


def summary(db: Session) -> list[VendorTrackingSummary]:
    """Every vendor, with how many profiles sit in each state for them.

    The sweep runs first and unscoped: this is the screen somebody opens to find
    out what lapsed, so it would be a poor one to serve stale holds from.
    """
    availability.expire_stale_reservations(db)
    db.flush()

    counts: dict[UUID, dict[str, int]] = {}
    for vendor_id, status, count in (
        db.query(
            ProfileVendorStatus.vendor_id,
            ProfileVendorStatus.status,
            func.count(ProfileVendorStatus.id),
        )
        .group_by(ProfileVendorStatus.vendor_id, ProfileVendorStatus.status)
        .all()
    ):
        counts.setdefault(vendor_id, {})[str(status)] = count

    # One rule for what counts as tracked, shared with the table below it: a
    # sidebar saying "1 profile" over a table with two rows in it is a bug
    # report waiting to be filed.
    tracked_pairs: dict[UUID, set[UUID]] = {}
    for vendor_id, person_id in _tracked_pairs(db):
        tracked_pairs.setdefault(vendor_id, set()).add(person_id)

    rows: list[VendorTrackingSummary] = []
    for vendor in db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.name).all():
        by_status = counts.get(vendor.id, {})
        rows.append(
            VendorTrackingSummary(
                vendor_id=vendor.id,
                vendor_name=vendor.name,
                tracked_profiles=len(tracked_pairs.get(vendor.id, ())),
                editing=by_status.get(str(ProfileVendorStatusEnum.EDITING), 0),
                reserved=by_status.get(str(ProfileVendorStatusEnum.RESERVED), 0),
                submitted=by_status.get(str(ProfileVendorStatusEnum.SUBMITTED), 0),
                engaged=by_status.get(str(ProfileVendorStatusEnum.ENGAGED), 0),
                released=by_status.get(str(ProfileVendorStatusEnum.RELEASED), 0),
                rejected=by_status.get(str(ProfileVendorStatusEnum.REJECTED), 0),
            )
        )
    return rows


def extend_pair_hold(
    db: Session, *, vendor_id: UUID, person_id: UUID, additional_minutes: int, actor_id: UUID | None = None
) -> None:
    """Put more time on this pair's RESERVED clock, for the Reservations screen.

    Adjusts the same reservation row the studio created — there is one hold per
    pair, and extending it is the only honest way to change how long it lasts.
    A pair with no active reservation (already released, or never one to begin
    with) has nothing here to extend, and is left alone rather than erroring:
    the caller sees no change in the refreshed list, which is the fact.
    """
    reservation = availability.active_reservation_for(db, person_id=person_id, vendor_id=vendor_id)
    if reservation is None:
        return
    availability.extend_reservation(
        db, reservation_id=reservation.id, additional_minutes=additional_minutes, actor_id=actor_id
    )


def release_orphan_hold(
    db: Session, *, vendor_id: UUID, person_id: UUID, actor_id: UUID | None = None
) -> None:
    """Let go of a hold that was reserved but never turned into a resume.

    A reservation and its composition session used to be created together in
    every path that mattered, so an EDITING or RESERVED status row always had
    a session behind it to Open or Delete. That stopped being true the moment
    a profile could be reserved on its own (see ``availability_controller``'s
    standalone reservations endpoint) without a session ever following: the
    row still shows up here — still holding the person against this vendor —
    but has nothing to open and nothing to delete, so an operator who no
    longer wants it had no way to let it go. This is that way: it exists only
    for a pair with no session at all, and refuses otherwise so it can never
    be used to erase a real composition's status out from under it.
    """
    if (
        db.query(ResumeCompositionSession)
        .filter(
            ResumeCompositionSession.vendor_id == vendor_id,
            ResumeCompositionSession.person_id == person_id,
        )
        .first()
        is not None
    ):
        raise ConflictError(
            "This pair has a resume session behind it — use Delete or Set status instead."
        )

    reservation = availability.active_reservation_for(db, person_id=person_id, vendor_id=vendor_id)
    if reservation is not None:
        availability.release_reservation(
            db, reservation_id=reservation.id, reason="MANUAL", actor_id=actor_id
        )
        return

    # No active reservation row, yet the status is not AVAILABLE (an even
    # older hold, or a status set by hand with nothing to point it back at):
    # there is nothing to release, so reset the status directly.
    status_row = availability.get_or_create_status(db, person_id, vendor_id)
    if status_row.status != ProfileVendorStatusEnum.AVAILABLE:
        status_row.status = ProfileVendorStatusEnum.AVAILABLE
        status_row.reserved_at = None
        status_row.reservation_expires_at = None
        status_row.current_requirement_id = None
        db.flush()


def vendor_detail(db: Session, vendor_id: UUID) -> VendorDetail:
    """One vendor's own page: what we sent them, who is working, who finished.

    Deliberately not a filter of ``tracked_profiles``. That view answers "where
    does each PERSON stand with this vendor" and so keeps one row per pair —
    the newest session, the newest submission. This answers "what has passed
    between us and this vendor", and for the submissions half that means every
    resume that ever went out, including three for the same person against
    three different JDs.
    """
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if vendor is None:
        raise NotFoundError("Vendor not found")

    submissions = (
        db.query(Submission)
        .filter(Submission.vendor_id == vendor_id)
        .order_by(Submission.created_at.desc())
        .all()
    )
    people = {
        p.id: p
        for p in db.query(Person).filter(
            Person.id.in_([s.person_id for s in submissions] or [None])
        )
    }
    requirements = {
        r.id: r
        for r in db.query(Requirement).filter(
            Requirement.id.in_([s.requirement_id for s in submissions] or [None])
        )
    }
    versions = {
        v.id: v
        for v in db.query(ResumeVersion).filter(
            ResumeVersion.id.in_([s.version_id for s in submissions] or [None])
        )
    }

    sent = [
        VendorSubmissionRow(
            submission_id=submission.id,
            reference_code=submission.reference_code,
            status=str(submission.status),
            person_id=submission.person_id,
            person_name=(
                people[submission.person_id].full_name
                if submission.person_id in people
                else "(removed)"
            ),
            requirement_id=submission.requirement_id,
            requirement_code=(
                requirements[submission.requirement_id].reference_code
                if submission.requirement_id in requirements
                else None
            ),
            role=(
                requirements[submission.requirement_id].parsed_role
                if submission.requirement_id in requirements
                else None
            ),
            version_no=(
                versions[submission.version_id].version_no
                if submission.version_id in versions
                else None
            ),
            submitted_at=as_aware(submission.submitted_at),
            created_at=as_aware(submission.created_at),
        )
        for submission in submissions
    ]

    # Engaged and past come off the status rows, because an engagement is a
    # fact about the pair rather than about any one submission.
    statuses = (
        db.query(ProfileVendorStatus)
        .filter(
            ProfileVendorStatus.vendor_id == vendor_id,
            ProfileVendorStatus.status.in_(
                [str(ProfileVendorStatusEnum.ENGAGED), str(ProfileVendorStatusEnum.RELEASED)]
            ),
        )
        .all()
    )
    status_people = {
        p.id: p
        for p in db.query(Person).filter(Person.id.in_([s.person_id for s in statuses] or [None]))
    }
    # The most recent thing each of them was put forward for, so a row carries
    # a JD rather than only a name.
    latest_for: dict[UUID, ResumeCompositionSession] = {}
    for session in (
        db.query(ResumeCompositionSession)
        .filter(
            ResumeCompositionSession.vendor_id == vendor_id,
            ResumeCompositionSession.person_id.in_([s.person_id for s in statuses] or [None]),
        )
        .order_by(ResumeCompositionSession.created_at.desc())
        .all()
    ):
        latest_for.setdefault(session.person_id, session)
    session_requirements = {
        r.id: r
        for r in db.query(Requirement).filter(
            Requirement.id.in_([s.requirement_id for s in latest_for.values()] or [None])
        )
    }

    def as_row(row: ProfileVendorStatus) -> VendorEngagementRow:
        session = latest_for.get(row.person_id)
        requirement = (
            session_requirements.get(session.requirement_id)
            if session and session.requirement_id
            else None
        )
        return VendorEngagementRow(
            person_id=row.person_id,
            person_name=(
                status_people[row.person_id].full_name
                if row.person_id in status_people
                else "(removed)"
            ),
            status=str(row.status),
            engagement_label=row.current_engagement_label,
            engagement_start=as_aware(row.engagement_start),
            engagement_end=as_aware(row.engagement_end),
            requirement_code=requirement.reference_code if requirement else None,
            role=requirement.parsed_role if requirement else None,
        )

    rows = [as_row(row) for row in statuses]
    return VendorDetail(
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        submissions=sent,
        engaged=[r for r in rows if r.status == str(ProfileVendorStatusEnum.ENGAGED)],
        past=[r for r in rows if r.status == str(ProfileVendorStatusEnum.RELEASED)],
    )


def reopen_released_pair(
    db: Session, *, vendor_id: UUID, person_id: UUID, actor_id: UUID | None = None
) -> None:
    """Put a released profile back in front of this vendor.

    RELEASED is a closed chapter by design — the person was taken and then let
    go, and nothing in the ordinary flow reopens it, which is why it does not
    lapse and is not selectable. That is the right default and the wrong
    absolute: sometimes the same vendor wants the same person back, and the
    alternative was to have no answer at all for that.

    So this is deliberate and manual, never automatic. The engagement dates
    stay exactly where they are — the fact that this person worked for this
    vendor between two dates does not stop being true because they are
    offerable again — and the audit trail records who reopened it.
    """
    status_row = availability.get_or_create_status(db, person_id, vendor_id)
    if str(status_row.status) != str(ProfileVendorStatusEnum.RELEASED):
        raise ConflictError(
            f"Only a released profile can be reopened; this one is {status_row.status}."
        )

    status_row.status = ProfileVendorStatusEnum.AVAILABLE
    status_row.reserved_at = None
    status_row.reservation_expires_at = None
    status_row.current_requirement_id = None
    status_row.last_interaction_at = utcnow()

    audit.record(
        db,
        action="RELEASE_REOPENED",
        entity_type="ProfileVendorStatus",
        entity_id=status_row.id,
        actor_id=actor_id,
        summary="Released profile made available to this vendor again",
        payload={
            "person_id": str(person_id),
            "vendor_id": str(vendor_id),
            # Kept, not cleared: what this person did for this vendor is a
            # fact about the past, and the next engagement is a new one.
            "previous_engagement_start": (
                status_row.engagement_start.isoformat() if status_row.engagement_start else None
            ),
            "previous_engagement_end": (
                status_row.engagement_end.isoformat() if status_row.engagement_end else None
            ),
        },
    )
    db.flush()


def set_pair_status(
    db: Session,
    *,
    vendor_id: UUID,
    person_id: UUID,
    status: ProfileVendorStatusEnum,
    engagement_label: str | None = None,
    notes: str | None = None,
    actor_id: UUID | None = None,
) -> None:
    """Move one (person, vendor) pair, and everything that describes it.

    Three records carry the same word — the availability row, the composition
    session, and the submission the resume went out on. Setting only the first
    is what let the History tab show RESERVED next to a vendor view showing
    ENGAGED. They move together or the vocabulary is decoration.

    The session and submission are only touched where the word applies to them:
    a session that already records a decision is not dragged back, and a pair
    with no submission simply has nothing to update.
    """
    availability.set_status(
        db,
        person_id=person_id,
        vendor_id=vendor_id,
        status=status,
        engagement_label=engagement_label,
        notes=notes,
        actor_id=actor_id,
    )

    session = (
        db.query(ResumeCompositionSession)
        .filter(
            ResumeCompositionSession.vendor_id == vendor_id,
            ResumeCompositionSession.person_id == person_id,
        )
        .order_by(ResumeCompositionSession.created_at.desc())
        .first()
    )
    if session is not None:
        session.status = ResumeSessionStatus(str(status))

        # created_at alone does not name a single row: approving twice in the
        # same tick gives two submissions the same timestamp, and "the newest"
        # then depends on whatever order the database feels like returning --
        # so this updated one submission while any other reader saw the other.
        submission = (
            db.query(Submission)
            .filter(Submission.session_id == session.id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .first()
        )
        if submission is not None:
            submission.status = SubmissionStatus(str(status))
            now = utcnow()
            if status == ProfileVendorStatusEnum.SUBMITTED and submission.submitted_at is None:
                submission.submitted_at = now
            if status in (ProfileVendorStatusEnum.REJECTED, ProfileVendorStatusEnum.RELEASED):
                submission.closed_at = now
    db.flush()


def tracked_profiles(db: Session, vendor_id: UUID | None = None) -> list[TrackedProfileRow]:
    """Every profile put forward, and where each one stands.

    ``vendor_id`` narrows it to one vendor; omitting it answers the question
    that actually matters once there are fifty of them — "what needs attention
    anywhere?" — which no amount of clicking through a sidebar will answer.

    Ordered by how much attention each row wants: the states somebody has to act
    on lead, and the settled ones sit underneath. Within a state the most
    recently touched comes first, which is the order the reader was already
    thinking in.
    """
    if vendor_id is not None and db.query(Vendor).filter(Vendor.id == vendor_id).first() is None:
        raise NotFoundError("Vendor not found")

    availability.expire_stale_reservations(db, vendor_id=vendor_id)
    db.flush()

    pairs = _tracked_pairs(db, vendor_id)
    if not pairs:
        return []
    vendor_ids = {v for v, _ in pairs}
    person_ids = {p for _, p in pairs}

    vendors = {v.id: v for v in db.query(Vendor).filter(Vendor.id.in_(vendor_ids)).all()}
    people = {p.id: p for p in db.query(Person).filter(Person.id.in_(person_ids)).all()}
    statuses = {
        (row.vendor_id, row.person_id): row
        for row in db.query(ProfileVendorStatus).filter(
            ProfileVendorStatus.vendor_id.in_(vendor_ids),
            ProfileVendorStatus.person_id.in_(person_ids),
        )
    }

    # Newest session per pair wins. Older ones are that pair's earlier attempts
    # and are reachable from the person drawer; repeating them here would turn a
    # state view into a log.
    latest_session: dict[tuple[UUID, UUID], ResumeCompositionSession] = {}
    for session in (
        db.query(ResumeCompositionSession)
        .filter(
            ResumeCompositionSession.vendor_id.in_(vendor_ids),
            ResumeCompositionSession.person_id.in_(person_ids),
        )
        .order_by(ResumeCompositionSession.created_at.desc())
        .all()
    ):
        latest_session.setdefault((session.vendor_id, session.person_id), session)

    session_ids = [s.id for s in latest_session.values()]
    submissions = {
        sub.session_id: sub
        for sub in db.query(Submission).filter(Submission.session_id.in_(session_ids or [None])).all()
    }
    requirement_ids = [s.requirement_id for s in latest_session.values() if s.requirement_id]
    requirements = {
        r.id: r
        for r in db.query(Requirement).filter(Requirement.id.in_(requirement_ids or [None]))
    }
    approved_counts: dict[tuple[UUID, UUID], int] = {
        (v, p): n
        for v, p, n in db.query(Submission.vendor_id, Submission.person_id, func.count(Submission.id))
        .filter(Submission.vendor_id.in_(vendor_ids), Submission.person_id.in_(person_ids))
        .group_by(Submission.vendor_id, Submission.person_id)
        .all()
    }

    now = utcnow()
    rows: list[TrackedProfileRow] = []
    for pair in pairs:
        vendor_key, person_id = pair
        person = people.get(person_id)
        vendor = vendors.get(vendor_key)
        if person is None or vendor is None:
            continue
        status_row = statuses.get(pair)
        session = latest_session.get(pair)
        submission = submissions.get(session.id) if session else None
        requirement = requirements.get(session.requirement_id) if session and session.requirement_id else None
        expires_at = as_aware(status_row.reservation_expires_at) if status_row else None

        rows.append(
            TrackedProfileRow(
                person_id=person.id,
                person_name=person.full_name,
                vendor_id=vendor.id,
                vendor_name=vendor.name,
                status=str(status_row.status) if status_row else str(ProfileVendorStatusEnum.AVAILABLE),
                role=(requirement.parsed_role if requirement else None),
                requirement_id=(requirement.id if requirement else None),
                requirement_code=(requirement.reference_code if requirement else None),
                session_id=session.id if session else None,
                session_reference=session.reference_code if session else None,
                session_status=str(session.status) if session else None,
                version_no=session.current_version_no if session else None,
                submission_reference=submission.reference_code if submission else None,
                submission_status=str(submission.status) if submission else None,
                approved_resumes=approved_counts.get(pair, 0),
                hold_expires_at=expires_at,
                # Precomputed so the browser is not doing clock arithmetic it
                # can get wrong across timezones.
                hold_minutes_left=(
                    max(0, int((expires_at - now).total_seconds() // 60)) if expires_at else None
                ),
                engagement_start=as_aware(status_row.engagement_start) if status_row else None,
                engagement_label=status_row.current_engagement_label if status_row else None,
                last_interaction_at=as_aware(status_row.last_interaction_at) if status_row else None,
            )
        )

    #: Needs-attention first. EDITING and RESERVED are somebody's open loop;
    #: ENGAGED is settled but live; the last three are closed.
    order = {
        str(ProfileVendorStatusEnum.RESERVED): 0,
        str(ProfileVendorStatusEnum.EDITING): 1,
        str(ProfileVendorStatusEnum.SUBMITTED): 2,
        str(ProfileVendorStatusEnum.ENGAGED): 3,
        str(ProfileVendorStatusEnum.RELEASED): 4,
        str(ProfileVendorStatusEnum.REJECTED): 5,
        str(ProfileVendorStatusEnum.AVAILABLE): 6,
    }
    rows.sort(
        key=lambda r: (
            order.get(r.status, 9),
            -(r.last_interaction_at.timestamp() if r.last_interaction_at else 0),
            r.vendor_name.lower(),
            r.person_name.lower(),
        )
    )
    return rows
