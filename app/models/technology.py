"""Sections 12/13 — Technology master and per-person technology records.

The master never stores mandatory/preferred (that is JD-specific, section 14)
and never stores years of experience (that is person-specific, section 13).
"""

from __future__ import annotations

from sqlalchemy import Column, Date, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, StringList


class Technology(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "technologies"

    name = Column(String(150), nullable=False, unique=True, index=True)
    category = Column(String(100), nullable=True, index=True)
    sub_category = Column(String(100), nullable=True)
    aliases = Column(StringList(), nullable=False, default=list)
    description = Column(Text, nullable=True)

    #: Section 25 — "framework availability / product release dates". Used by the
    #: validation engine to detect a technology claimed before it existed.
    first_release_date = Column(Date, nullable=True)

    person_links = relationship("PersonTechnology", back_populates="technology")

    def __repr__(self) -> str:
        return f"<Technology {self.name}>"


class PersonTechnology(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 13 — the same technology has different depth per person."""

    __tablename__ = "person_technologies"
    __table_args__ = (
        UniqueConstraint("person_id", "technology_id", name="uq_person_technology"),
        Index("ix_person_technology_person", "person_id"),
    )

    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    technology_id = Column(GUID(), ForeignKey("technologies.id", ondelete="RESTRICT"), nullable=False)
    years_of_experience = Column(Float, nullable=True)
    proficiency = Column(String(20), nullable=True)
    is_primary = Column(String(10), nullable=True)
    notes = Column(Text, nullable=True)

    person = relationship("Person", back_populates="technologies")
    technology = relationship("Technology", back_populates="person_links")
