"""Section 22 — every chatbot turn is persisted against the session."""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType


class AIInteraction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_interactions"

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: user | assistant | system
    role = Column(String(20), nullable=False)
    message = Column(Text, nullable=True)
    #: Structured record of the edit tools the agent invoked this turn.
    tool_calls = Column(JSONType(), nullable=True)
    resulting_version_id = Column(GUID(), ForeignKey("resume_versions.id", ondelete="SET NULL"), nullable=True)

    model = Column(String(100), nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="interactions")
