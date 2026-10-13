"""Section 33 — a submission's status.

The same vocabulary as everything else: see ``app/utils/enums/profile_status.py``
for the lifecycle. A submission is created when a resume is approved, so it
starts at RESERVED and moves as somebody decides what to do with it.

This used to be a ladder of its own (UNDER_REVIEW, INTERVIEW, SELECTED…) mapped
onto the availability states by a lookup table. Two vocabularies for one
situation meant the History tab and the Vendor tracking module could describe
the same submission differently, and the table between them was one more thing
to keep true. There is now one set of names and no mapping to get wrong.
"""

from __future__ import annotations

from enum import StrEnum


class SubmissionStatus(StrEnum):
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    ENGAGED = "ENGAGED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


#: Submission status -> resulting ProfileVendorStatus. Kept as a name because
#: call sites read better for it, but it is the identity now: the two enums are
#: the same words, which is the point.
SUBMISSION_TO_PROFILE_STATUS: dict[str, str] = {
    SubmissionStatus.RESERVED: "RESERVED",
    SubmissionStatus.SUBMITTED: "SUBMITTED",
    SubmissionStatus.ENGAGED: "ENGAGED",
    SubmissionStatus.RELEASED: "RELEASED",
    SubmissionStatus.REJECTED: "REJECTED",
}
