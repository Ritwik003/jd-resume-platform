"""Sections 8/9/29/31 — vendor-scoped availability and reservations.

The unique constraint on (person_id, vendor_id) is the entire availability
model: the same person is tracked independently against every vendor.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID
from app.utils.enums import ProfileVendorStatusEnum


class ProfileVendorStatus(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "profile_vendor_statuses"
    __table_args__ = (
        UniqueConstraint("person_id", "vendor_id", name="uq_profile_vendor"),
        Index("ix_profile_vendor_lookup", "vendor_id", "status"),
    )

    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    vendor_id = Column(GUID(), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False)

    status = Column(String(20), nullable=False, default=ProfileVendorStatusEnum.AVAILABLE)
    reserved_at = Column(DateTime(timezone=True), nullable=True)
    reservation_expires_at = Column(DateTime(timezone=True), nullable=True)
    engagement_start = Column(DateTime(timezone=True), nullable=True)
    engagement_end = Column(DateTime(timezone=True), nullable=True)
    last_interaction_at = Column(DateTime(timezone=True), nullable=True)

    current_requirement_id = Column(
        GUID(), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    #: Free-text engagement note, e.g. "Client A — GenAI" (section 7 table).
    current_engagement_label = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    person = relationship("Person", back_populates="vendor_statuses")
    vendor = relationship("Vendor", back_populates="profile_statuses")
    current_requirement = relationship("Requirement")

    @property
    def vendor_name(self) -> str | None:
        """The vendor this status is about, for the API to read directly.

        A status without its vendor is not usable information: "ENGAGED" is only
        meaningful as "ENGAGED with Apex", because the same person is
        simultaneously AVAILABLE to everyone else.
        """
        return self.vendor.name if self.vendor else None

    def __repr__(self) -> str:
        return f"<ProfileVendorStatus person={self.person_id} vendor={self.vendor_id} {self.status}>"


class ProfileReservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 9 — an auditable log of every reservation, including expiries."""

    __tablename__ = "profile_reservations"
    __table_args__ = (Index("ix_reservation_active", "person_id", "vendor_id", "released_at"),)

    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    vendor_id = Column(GUID(), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False)
    requirement_id = Column(GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=True)

    reserved_at = Column(DateTime(timezone=True), nullable=False)
    #: NULL means no clock — the open-ended EDITING hold. RESERVED always sets
    #: this; EDITING never does, so an abandoned tab never expires on its own.
    expires_at = Column(DateTime(timezone=True), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    #: MANUAL | EXPIRED | CONVERTED_TO_ENGAGEMENT
    release_reason = Column(String(40), nullable=True)
    reserved_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    person = relationship("Person")
    vendor = relationship("Vendor")
    requirement = relationship("Requirement")
