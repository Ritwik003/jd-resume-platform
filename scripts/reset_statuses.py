"""Reset every status in the database to the single vocabulary.

The platform used to carry three overlapping status ladders — one for sessions
(DRAFT/GENERATING/GENERATED/APPROVED/ABANDONED), one for submissions
(UNDER_REVIEW/INTERVIEW/SELECTED) and one for availability. Rows written under
those still hold words that no longer exist, and a row whose status is not a
member of its enum renders as an unknown chip and cannot be filtered to.

This puts everything on the current flow:

    AVAILABLE ─select─▶ EDITING ─approve─▶ RESERVED ─┬─▶ SUBMITTED
                                                     ├─▶ ENGAGED
                                                     ├─▶ RELEASED
                                                     └─▶ REJECTED

What it does:

  * every composition session  -> EDITING
  * every (person, vendor) row -> AVAILABLE, holds and engagements cleared
  * every reservation still open -> released, reason RESET
  * every submission           -> DELETED, along with its status history
  * every APPROVED resume version -> DRAFT

**What it deliberately does not touch: the resumes themselves.** Version rows,
their content and their rendered files all survive. "Remove the approved
resumes" means removing the approvals — the fact that something was signed off
and sent — not the compositions people built, which are exactly what "keep every
session on editing" asks to keep.

Run it with ``python scripts/reset_statuses.py``. It prints what it changed and
asks before writing unless ``--yes`` is passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.database import SessionLocal  # noqa: E402
from app.model_registry import import_all_models  # noqa: E402
from app.models.availability import ProfileReservation, ProfileVendorStatus  # noqa: E402
from app.models.resume_session import ResumeCompositionSession  # noqa: E402
from app.models.resume_version import ResumeVersion  # noqa: E402
from app.models.submission import Submission, SubmissionStatusHistory  # noqa: E402
from app.utils.enums import ProfileVendorStatusEnum, ResumeSessionStatus, ResumeVersionStatus  # noqa: E402
from app.utils.helper import utcnow  # noqa: E402


def reset(*, confirm: bool = True) -> dict[str, int]:
    import_all_models()
    changed: dict[str, int] = {}

    with SessionLocal() as db:
        sessions = db.query(ResumeCompositionSession).all()
        statuses = db.query(ProfileVendorStatus).all()
        reservations = (
            db.query(ProfileReservation).filter(ProfileReservation.released_at.is_(None)).all()
        )
        submissions = db.query(Submission).all()
        approved = (
            db.query(ResumeVersion)
            .filter(ResumeVersion.status == str(ResumeVersionStatus.APPROVED))
            .all()
        )

        plan = {
            "sessions -> EDITING": len(sessions),
            "vendor statuses -> AVAILABLE": len(statuses),
            "open reservations -> released": len(reservations),
            "submissions deleted": len(submissions),
            "approved versions -> DRAFT": len(approved),
        }
        print("This will change:")
        for label, count in plan.items():
            print(f"  {count:>5}  {label}")
        print("\nResume versions, their content and their rendered files are kept.")

        if confirm:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing was written.")
                return {}

        now = utcnow()

        for session in sessions:
            session.status = ResumeSessionStatus.EDITING

        for row in statuses:
            row.status = ProfileVendorStatusEnum.AVAILABLE
            row.reserved_at = None
            row.reservation_expires_at = None
            row.engagement_start = None
            row.engagement_end = None
            row.current_requirement_id = None
            row.current_engagement_label = None
            row.last_interaction_at = now

        for reservation in reservations:
            reservation.released_at = now
            reservation.release_reason = "RESET"

        # History first: it references the submission it describes.
        history = db.query(SubmissionStatusHistory).delete(synchronize_session=False)
        for submission in submissions:
            db.delete(submission)

        for version in approved:
            version.status = ResumeVersionStatus.DRAFT

        db.commit()
        changed = {**plan, "submission history rows deleted": history}

    return changed


if __name__ == "__main__":
    result = reset(confirm="--yes" not in sys.argv)
    if result:
        print("\nDone:")
        for label, count in result.items():
            print(f"  {count:>5}  {label}")
