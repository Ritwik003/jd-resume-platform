"""Section 15 — reusable Project pool, with ProjectTechnology.

A project is a standalone capability record — a name, a role, a description,
the technologies it evidences. It is not tied to any person or any company:
the bank is one shared pool, and which project answers a given JD is a
question of how well its ``role`` and content match, never of who is asking
or who is picked alongside it.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, StringList


class Project(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "projects"

    name = Column(String(255), nullable=False)
    role = Column(String(255), nullable=True)

    short_description = Column(Text, nullable=True)
    detailed_description = Column(Text, nullable=True)
    responsibilities = Column(StringList(), nullable=False, default=list)
    keywords = Column(StringList(), nullable=False, default=list)

    technologies = relationship("ProjectTechnology", back_populates="project", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Project {self.name}>"


class ProjectTechnology(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_technologies"
    __table_args__ = (UniqueConstraint("project_id", "technology_id", name="uq_project_technology"),)

    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    technology_id = Column(GUID(), ForeignKey("technologies.id", ondelete="RESTRICT"), nullable=False)
    is_core = Column(String(10), nullable=True)

    project = relationship("Project", back_populates="technologies")
    technology = relationship("Technology")
