"""Availability, reservation and candidate-listing DTOs (sections 7–9, 29–31)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel
from app.utils.enums import ProfileVendorStatusEnum


class AvailableProfileRow(BaseModel):
    """One row of the section 7 candidate table.

    Deliberately carries no score or rank — section 7: "The system does not need
    to rank candidates" and section 42 puts candidate ranking out of scope.

    It also carries no role and no skills, because a person has neither. A
    person is an identity; the role comes from the JD they are being selected
    for, and what a resume claims is composed for that JD out of the shared
    bank. A "base role" column could only show whatever they last went out as,
    which is a different JD's answer to a question this screen is not asking,
    and a "skills" column could only report a per-person skill record that is
    no longer kept. Both read as facts about the candidate while being neither.

    What is left is the only thing this table has to decide: who is free for
    this vendor.
    """

    person_id: UUID
    person_name: str
    #: Who currently has this person working, across ALL vendors — the one
    #: field here not scoped to the vendor being listed. Someone ENGAGED with
    #: Apex is still selectable for Kestrel (§8), and this is how the person
    #: choosing sees that before they choose. Empty means nobody has them.
    current_engagement_label: str | None = None
    vendor_status: str
    is_selectable: bool
    reservation_expires_at: datetime | None = None


class ProfileVendorStatusResponse(ORMModel):
    id: UUID
    person_id: UUID
    vendor_id: UUID
    #: Read through the relationship. A status row is meaningless without the
    #: vendor it belongs to — "ENGAGED" alone says nothing, "ENGAGED with Apex"
    #: is the fact — so the caller should never have to make a second request
    #: just to render the state.
    vendor_name: str | None = None
    status: str
    reserved_at: datetime | None = None
    reservation_expires_at: datetime | None = None
    engagement_start: datetime | None = None
    engagement_end: datetime | None = None
    last_interaction_at: datetime | None = None
    current_engagement_label: str | None = None
    current_requirement_id: UUID | None = None


class StatusOverrideRequest(BaseModel):
    status: ProfileVendorStatusEnum
    current_engagement_label: str | None = None
    notes: str | None = None


class ReservationCreate(BaseModel):
    person_id: UUID
    vendor_id: UUID
    requirement_id: UUID
    #: Unset means no clock — the default open-ended EDITING hold. Pass this
    #: only when the caller specifically wants a time-boxed hold instead.
    duration_minutes: int | None = None


class ReservationResponse(ORMModel):
    id: UUID
    person_id: UUID
    vendor_id: UUID
    requirement_id: UUID | None = None
    reserved_at: datetime
    #: NULL is an open-ended EDITING hold with no clock. Only an approved
    #: (RESERVED) hold ever sets this.
    expires_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        """Section 9 renders a reservation with an explicit status.

        Derived rather than stored so an expired reservation reads as EXPIRED
        the moment it lapses, even before the sweeper releases it. No expiry
        at all means the open-ended EDITING hold, which reads as RESERVED —
        still held, just without a deadline.
        """
        if self.released_at is not None:
            return "RELEASED"
        if self.expires_at is None:
            return "RESERVED"
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return "RESERVED" if expires > datetime.now(UTC) else "EXPIRED"


class ReservationExtendRequest(BaseModel):
    additional_minutes: int = 60
