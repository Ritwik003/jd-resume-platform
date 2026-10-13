"""Sections 28/33 — vendor submissions and their status history."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID
from app.utils.enums import SubmissionStatus


class Submission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "submissions"

    reference_code = Column(String(50), nullable=True, unique=True, index=True)

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_id = Column(GUID(), ForeignKey("resume_versions.id", ondelete="RESTRICT"), nullable=False)
    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False, index=True)
    vendor_id = Column(GUID(), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True)
    requirement_id = Column(GUID(), ForeignKey("requirements.id", ondelete="RESTRICT"), nullable=False)

    status = Column(String(20), nullable=False, default=SubmissionStatus.RESERVED)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    created_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    session = relationship("ResumeCompositionSession")
    version = relationship("ResumeVersion")
    person = relationship("Person")
    vendor = relationship("Vendor")
    requirement = relationship("Requirement")
    status_history = relationship(
        "SubmissionStatusHistory", back_populates="submission", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Submission {self.reference_code or self.id} {self.status}>"


class SubmissionStatusHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "submission_status_history"

    submission_id = Column(GUID(), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=False)
    changed_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)
    reason = Column(Text, nullable=True)

    submission = relationship("Submission", back_populates="status_history")
