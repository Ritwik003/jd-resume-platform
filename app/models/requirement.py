"""Section 32 — every JD becomes a stored Requirement. The raw JD is retained."""

from __future__ import annotations

from sqlalchemy import Column, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType, StringList
from app.utils.enums import RequirementStatus


class Requirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "requirements"

    reference_code = Column(String(50), nullable=True, unique=True, index=True)
    vendor_id = Column(GUID(), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True)

    #: The template chosen alongside the vendor when the JD was pasted. Every
    #: resume session for this JD inherits it unless the user overrides.
    template_id = Column(
        GUID(), ForeignKey("resume_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Section 32 — "The original JD should always be retained."
    raw_jd_text = Column(Text, nullable=False)

    parsed_role = Column(String(255), nullable=True)
    min_experience_years = Column(Float, nullable=True)
    max_experience_years = Column(Float, nullable=True)
    location = Column(String(255), nullable=True)
    domain = Column(String(255), nullable=True)
    responsibilities = Column(StringList(), nullable=False, default=list)
    other_requirements = Column(StringList(), nullable=False, default=list)

    #: JD skills that named something the technology master has no row for at
    #: all — distinct from a skill the master knows but this resume doesn't
    #: evidence. Nothing can close this gap short of adding the technology.
    unmatched_mandatory_skills = Column(StringList(), nullable=False, default=list)
    unmatched_preferred_skills = Column(StringList(), nullable=False, default=list)

    #: Full parser output kept verbatim for traceability and re-parsing.
    parsed_payload = Column(JSONType(), nullable=True)
    parse_model = Column(String(100), nullable=True)
    is_parsed = Column(String(10), nullable=True)
    #: True once a human has reviewed/corrected the parse (section 5).
    is_confirmed = Column(String(10), nullable=True)

    status = Column(String(30), nullable=False, default=RequirementStatus.OPEN)
    created_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    vendor = relationship("Vendor", back_populates="requirements")
    template = relationship("ResumeTemplate")
    technologies = relationship(
        "RequirementTechnology", back_populates="requirement", cascade="all, delete-orphan"
    )
    sessions = relationship("ResumeCompositionSession", back_populates="requirement")

    def __repr__(self) -> str:
        return f"<Requirement {self.reference_code or self.id} {self.parsed_role}>"


class RequirementTechnology(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 14 — mandatory/preferred is a property of the JD, not the technology."""

    __tablename__ = "requirement_technologies"
    __table_args__ = (UniqueConstraint("requirement_id", "technology_id", name="uq_requirement_technology"),)

    requirement_id = Column(GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    technology_id = Column(GUID(), ForeignKey("technologies.id", ondelete="RESTRICT"), nullable=False)
    importance = Column(String(20), nullable=False, default="MANDATORY")
    #: Raw token from the JD before alias resolution, for audit of the parse.
    raw_label = Column(String(150), nullable=True)

    requirement = relationship("Requirement", back_populates="technologies")
    technology = relationship("Technology")
