"""Sections 24/25 — validation assistant. Every finding is a warning the user
decides on (Keep/Modify/Dismiss); approval itself then requires that every
warning on the current version carry a decision — see
``resume_session_service.approve``."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType
from app.utils.enums import ValidationSeverity, ValidationWarningStatus


class ValidationWarning(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "validation_warnings"

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id = Column(GUID(), ForeignKey("resume_versions.id", ondelete="CASCADE"), nullable=True, index=True)

    warning_type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False, default=ValidationSeverity.WARNING)
    message = Column(Text, nullable=False)
    #: Where in content_json the issue lives, e.g. "projects[1].description".
    content_path = Column(String(255), nullable=True)
    claim_text = Column(Text, nullable=True)
    evidence = Column(JSONType(), nullable=True)

    status = Column(String(20), nullable=False, default=ValidationWarningStatus.OPEN)
    decided_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="warnings")
    version = relationship("ResumeVersion", back_populates="warnings")

    def __repr__(self) -> str:
        return f"<ValidationWarning {self.warning_type} {self.status}>"
