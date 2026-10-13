"""Append-only audit trail.

Not in section 39, but sections 18/23/25 place final responsibility on the
human, which only means something if there is a durable record of who decided
what. Every approval, status transition and company re-attribution writes here.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    #: e.g. RESUME_APPROVED, COMPANY_REATTRIBUTED, SUBMISSION_STATUS_CHANGED
    action = Column(String(60), nullable=False, index=True)
    entity_type = Column(String(60), nullable=False, index=True)
    entity_id = Column(GUID(), nullable=True, index=True)
    actor_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)
    summary = Column(Text, nullable=True)
    payload = Column(JSONType(), nullable=True)

    actor = relationship("User")

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}>"
