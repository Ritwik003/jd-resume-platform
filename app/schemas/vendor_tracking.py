"""Section 29 — DTOs for the vendor tracking view.

One row per (person, vendor) pair, assembled from the status row, the newest
composition session and its submission. Everything the screen prints is on the
row: it is a monitoring view, and a table that needs a second request per line
to render is one that stops being opened.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.utils.enums import ProfileVendorStatusEnum


class VendorTrackingSummary(BaseModel):
    """One vendor, and how many profiles sit in each state for them."""

    vendor_id: UUID
    vendor_name: str
    #: Distinct people ever put forward to this vendor.
    tracked_profiles: int
    editing: int = 0
    reserved: int = 0
    submitted: int = 0
    engaged: int = 0
    released: int = 0
    rejected: int = 0

    @property
    def needs_attention(self) -> int:
        """Open loops: a composition nobody finished, a hold nobody answered."""
        return self.editing + self.reserved


class TrackedProfileRow(BaseModel):
    """Where one person stands with one vendor, and what it is about."""

    person_id: UUID
    person_name: str
    status: str

    #: Carried on every row, not just the cross-vendor listing. At fifty
    #: vendors the useful default view is "everything that needs attention",
    #: which is a list of rows from many vendors at once — and a row that
    #: cannot say which vendor it is about is useless in it.
    vendor_id: UUID
    vendor_name: str

    #: What they were put forward for. Null when the pair only exists because
    #: somebody set a status by hand.
    role: str | None = None
    requirement_id: UUID | None = None
    requirement_code: str | None = None

    session_id: UUID | None = None
    session_reference: str | None = None
    session_status: str | None = None
    version_no: int | None = None

    submission_reference: str | None = None
    submission_status: str | None = None
    #: Approved resumes sent to this vendor for this person, across all sessions.
    approved_resumes: int = 0

    #: When the current hold lapses. Only EDITING and RESERVED carry one.
    hold_expires_at: datetime | None = None
    #: Minutes left on that hold, computed server-side so the browser is not
    #: doing timezone arithmetic it can get wrong.
    hold_minutes_left: int | None = None

    engagement_start: datetime | None = None
    engagement_label: str | None = None
    last_interaction_at: datetime | None = None


class VendorSubmissionRow(BaseModel):
    """One resume that went to this vendor, and which JD it answered.

    Every submission ever made, not the latest per person: this is the record
    of what was sent, so a person put forward three times appears three times.
    """

    submission_id: UUID
    reference_code: str | None = None
    status: str

    person_id: UUID
    person_name: str

    #: The JD it answered — its reference code is what an operator quotes to a
    #: vendor, and the role is what makes the code mean something on the page.
    requirement_id: UUID | None = None
    requirement_code: str | None = None
    role: str | None = None

    version_no: int | None = None
    submitted_at: datetime | None = None
    created_at: datetime | None = None


class VendorEngagementRow(BaseModel):
    """One person's engagement with this vendor — running, or finished."""

    person_id: UUID
    person_name: str
    status: str
    engagement_label: str | None = None
    engagement_start: datetime | None = None
    #: Set only once the engagement ended, which is what makes it "past".
    engagement_end: datetime | None = None
    #: What they were last put forward for, so a row is not just a name.
    requirement_code: str | None = None
    role: str | None = None


class VendorDetail(BaseModel):
    """Everything one vendor's page shows, in one request.

    Three lists rather than three endpoints: they are read together, always,
    and the page would otherwise flash in three stages.
    """

    vendor_id: UUID
    vendor_name: str
    submissions: list[VendorSubmissionRow] = []
    engaged: list[VendorEngagementRow] = []
    past: list[VendorEngagementRow] = []


class ExtendHoldRequest(BaseModel):
    additional_minutes: int = 60


class TrackingStatusChange(BaseModel):
    """Advance one (person, vendor) pair by hand from the tracking view."""

    status: ProfileVendorStatusEnum
    notes: str | None = None
    engagement_label: str | None = None
