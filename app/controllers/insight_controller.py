"""Drill-downs across the knowledge base (sections 28, 29, 34).

The list screens answer "what do we have"; these answer the question a user asks
next, which is always "…and what does that connect to". Each one is a single
query with the ordering the question implies — a role's resume history newest
first, a person's the same, a technology sorted by depth in that technology —
because sorting client-side after five round trips is how a fast page becomes a
slow one.

None of them ranks candidates against each other. Section 7 is explicit that
the system does not need to rank, and a list of people ordered by seniority is
a ranking however it is captioned, so a role answers with what was built under
it rather than with who is best at it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.availability import ProfileReservation
from app.models.person import Person, PersonProfile, ProfileType
from app.models.requirement import Requirement
from app.models.resume_session import ResumeCompositionSession
from app.models.resume_version import ResumeFile, ResumeVersion
from app.models.submission import Submission
from app.models.technology import PersonTechnology, Technology
from app.models.user import User
from app.models.vendor import Vendor
from app.repositories.base_repository import get_or_404
from app.utils.helper import utcnow

router = APIRouter()


# ─────────────────────────── a role, and every resume built under it
@router.get("/profile-types/{profile_type_id}/resumes")
def resumes_for_profile_type(
    profile_type_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Every resume composed under this role — who it was for, and for whom.

    A role arrives in a JD and is logged here (see ``app/services/roles.py``),
    so the question it raises is what came of it: which people we put forward
    under this title, to which vendors, and what state each of those went to.

    Deliberately not a leaderboard of the people who hold the role. Ordering
    candidates was never something this platform does — section 7 says the
    system does not rank them and section 42 puts ranking out of scope — and a
    list sorted by career length is a ranking whatever it is labelled. What is
    useful about a role is its history, so that is what this returns: newest
    first, the order somebody reading it is already thinking in.
    """
    profile_type = get_or_404(db, ProfileType, profile_type_id, "Profile type")

    rows = (
        db.query(ResumeCompositionSession, Person, Vendor, Requirement)
        .join(PersonProfile, PersonProfile.id == ResumeCompositionSession.person_profile_id)
        .join(Person, Person.id == ResumeCompositionSession.person_id)
        .join(Vendor, Vendor.id == ResumeCompositionSession.vendor_id)
        .outerjoin(Requirement, Requirement.id == ResumeCompositionSession.requirement_id)
        .filter(PersonProfile.profile_type_id == profile_type_id)
        .order_by(ResumeCompositionSession.created_at.desc())
        .all()
    )

    # The downloadable artefacts of each session's current version, in one
    # query rather than one per row.
    version_ids = [s.current_version_id for s, _, _, _ in rows if s.current_version_id]
    files_by_version: dict[UUID, list[dict]] = {}
    for file in db.query(ResumeFile).filter(ResumeFile.version_id.in_(version_ids or [None])).all():
        files_by_version.setdefault(file.version_id, []).append(
            {"id": str(file.id), "file_format": file.file_format, "file_name": file.file_name}
        )

    return {
        "profile_type": profile_type.name,
        "count": len(rows),
        "resumes": [
            {
                "session_id": str(session.id),
                "reference_code": session.reference_code,
                "person_id": str(person.id),
                "person_name": person.full_name,
                "vendor_name": vendor.name,
                "requirement_code": requirement.reference_code if requirement else None,
                "role": requirement.parsed_role if requirement else None,
                "status": str(session.status),
                "version_no": session.current_version_no,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "files": files_by_version.get(session.current_version_id, []),
            }
            for session, person, vendor, requirement in rows
        ],
    }


# ──────────────────────────── a technology, and who is deepest in it
@router.get("/technologies/{technology_id}/people")
def people_for_technology(
    technology_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Everyone who knows this technology, deepest first.

    Ordered on the person's years *in this technology* — the number assessed
    when their profile was imported (section 13). It is the only years figure
    the platform keeps, and deliberately so: someone who touched Kubernetes once
    should not outrank someone who has run it for four years.
    """
    technology = get_or_404(db, Technology, technology_id, "Technology")
    rows = (
        db.query(PersonTechnology, Person)
        .join(Person, Person.id == PersonTechnology.person_id)
        .filter(
            PersonTechnology.technology_id == technology_id,
            Person.is_active.is_(True),
        )
        .all()
    )
    ordered = sorted(
        rows,
        key=lambda pair: (
            float(pair[0].years_of_experience or 0),
            str(pair[0].is_primary).lower() == "true",
        ),
        reverse=True,
    )
    return {
        "technology": technology.name,
        "category": technology.category,
        "first_release_date": (
            technology.first_release_date.isoformat() if technology.first_release_date else None
        ),
        "count": len(ordered),
        "people": [
            {
                "person_id": str(person.id),
                "full_name": person.full_name,
                "current_designation": person.current_designation,
                "years_of_experience": link.years_of_experience,
                "proficiency": link.proficiency,
                "is_primary": str(link.is_primary).lower() == "true",
                "basis": link.notes,
            }
            for link, person in ordered
        ],
    }


# ──────────────────────────── a person, and every resume made for them
@router.get("/profiles/{person_id}/resumes")
def resumes_for_person(
    person_id: UUID,
    include_drafts: bool = Query(
        False, description="Also return sessions that were never approved."
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Section 28 — the vendor-specific resume history for one person.

    Only resumes that were **approved** count as history. A session that was
    started and abandoned produced no document and was never shown to anyone;
    listing it alongside real submissions makes the history look busier than it
    is and puts an un-downloadable row next to a downloadable one.

    Approval is detected by the Submission row, not the session status: a
    submission is created at the moment of approval and carries the
    vendor-facing outcome (SUBMITTED, REJECTED, SELECTED), which is the status
    section 28's own example prints.

    ``include_drafts`` brings the unfinished sessions back for anyone auditing
    work in progress.

    Assembled in one place rather than in the browser: the alternative is a
    request per session plus one per version, which is four round trips to
    answer what is really one join.
    """
    person = get_or_404(db, Person, person_id, "Person")
    query = (
        db.query(ResumeCompositionSession, Vendor, Requirement, Submission)
        .outerjoin(Vendor, Vendor.id == ResumeCompositionSession.vendor_id)
        .outerjoin(Requirement, Requirement.id == ResumeCompositionSession.requirement_id)
        .outerjoin(Submission, Submission.session_id == ResumeCompositionSession.id)
        .filter(ResumeCompositionSession.person_id == person_id)
    )
    if not include_drafts:
        query = query.filter(Submission.id.isnot(None))

    sessions = query.order_by(ResumeCompositionSession.created_at.desc()).all()
    if not sessions:
        return {"person": person.full_name, "count": 0, "resumes": []}

    session_ids = [s.id for s, _v, _r, _sub in sessions]
    # Files belong to versions; fetch them all at once and group in memory.
    file_rows = (
        db.query(ResumeFile, ResumeVersion)
        .join(ResumeVersion, ResumeVersion.id == ResumeFile.version_id)
        .filter(ResumeVersion.session_id.in_(session_ids))
        .all()
    )
    by_session: dict[UUID, list] = {}
    for file, version in file_rows:
        by_session.setdefault(version.session_id, []).append((file, version))

    version_counts = dict(
        db.query(ResumeVersion.session_id, func.count(ResumeVersion.id))
        .filter(ResumeVersion.session_id.in_(session_ids))
        .group_by(ResumeVersion.session_id)
        .all()
    )

    resumes = []
    for session, vendor, requirement, submission in sessions:
        files = by_session.get(session.id, [])
        # Only the newest version's artefacts are worth offering; the older ones
        # are superseded and downloading them by accident is a real mistake.
        latest = max((v.version_no for _f, v in files), default=None)
        resumes.append(
            {
                "session_id": str(session.id),
                "reference_code": submission.reference_code if submission else session.reference_code,
                "vendor_name": vendor.name if vendor else None,
                "role": (requirement.parsed_role if requirement else None),
                # The vendor-facing outcome when there is one; the session's own
                # state only for a draft the caller explicitly asked to see.
                "status": submission.status if submission else session.status,
                "is_approved": submission is not None,
                "submitted_at": submission.submitted_at if submission else None,
                "approved_at": submission.approved_at if submission else None,
                "fit_score": session.fit_score,
                "version_no": session.current_version_no,
                "version_count": version_counts.get(session.id, 0),
                "created_at": session.created_at,
                "files": [
                    {
                        "id": str(f.id),
                        "file_format": f.file_format,
                        "file_name": f.file_name,
                        "size_bytes": f.size_bytes,
                    }
                    for f, v in files
                    if v.version_no == latest
                ],
            }
        )
    return {"person": person.full_name, "count": len(resumes), "resumes": resumes}


# ─────────────────────────────────────── reservations currently held
@router.get("/reservations")
def list_reservations(
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Section 9/31 — who is locked to whom, and until when.

    Without a list of these, an expired-but-uncleared reservation is invisible
    until someone tries to select the person and is told they cannot.
    """
    query = (
        db.query(ProfileReservation, Person, Vendor)
        .outerjoin(Person, Person.id == ProfileReservation.person_id)
        .outerjoin(Vendor, Vendor.id == ProfileReservation.vendor_id)
    )
    if active_only:
        query = query.filter(ProfileReservation.released_at.is_(None))

    now = utcnow()
    rows = query.order_by(ProfileReservation.expires_at.desc()).limit(200).all()
    return {
        "count": len(rows),
        "reservations": [
            {
                "id": str(res.id),
                "person_id": str(res.person_id),
                "person_name": person.full_name if person else None,
                "vendor_id": str(res.vendor_id),
                "vendor_name": vendor.name if vendor else None,
                "requirement_id": str(res.requirement_id) if res.requirement_id else None,
                "reserved_at": res.reserved_at,
                "expires_at": res.expires_at,
                "released_at": res.released_at,
                "release_reason": res.release_reason,
                # Computed here so every client agrees on what "expired" means.
                "is_expired": bool(
                    res.released_at is None
                    and res.expires_at is not None
                    and res.expires_at.replace(tzinfo=res.expires_at.tzinfo or now.tzinfo) <= now
                ),
            }
            for res, person, vendor in rows
        ],
    }
