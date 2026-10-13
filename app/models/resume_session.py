"""Section 20 — Resume Composition Session and its selection tables.

The session stores *pointers plus overrides*, never copies. Master data is
therefore never mutated by a resume run (section 17: "This configuration belongs
to the resume session and does not modify the underlying master pool.").
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, StringList
from app.utils.enums import ResumeSessionStatus


class ResumeCompositionSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_composition_sessions"

    reference_code = Column(String(50), nullable=True, unique=True, index=True)

    requirement_id = Column(GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    vendor_id = Column(GUID(), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False)
    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False, index=True)
    person_profile_id = Column(GUID(), ForeignKey("person_profiles.id", ondelete="RESTRICT"), nullable=False)
    template_id = Column(GUID(), ForeignKey("resume_templates.id", ondelete="RESTRICT"), nullable=False)
    reservation_id = Column(GUID(), ForeignKey("profile_reservations.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(20), nullable=False, default=ResumeSessionStatus.EDITING)
    created_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    #: Pointer to the version currently shown in the studio.
    current_version_id = Column(GUID(), nullable=True)
    current_version_no = Column(Integer, nullable=False, default=0)

    #: How well the chosen profile already matched the JD, 0-100, recorded at
    #: generation. At or above the configured threshold the stored history is
    #: transformed into the template unchanged; below it, the history is moulded
    #: toward the role first. Kept so a reader can see which of the two happened.
    fit_score = Column(Integer, nullable=True)
    fit_basis = Column(Text, nullable=True)

    requirement = relationship("Requirement", back_populates="sessions")
    vendor = relationship("Vendor")
    person = relationship("Person")
    person_profile = relationship("PersonProfile")
    template = relationship("ResumeTemplate")

    selected_projects = relationship(
        "ResumeSelectedProject", back_populates="session", cascade="all, delete-orphan"
    )
    selected_companies = relationship(
        "ResumeSelectedCompany", back_populates="session", cascade="all, delete-orphan"
    )
    selected_technologies = relationship(
        "ResumeSelectedTechnology", back_populates="session", cascade="all, delete-orphan"
    )
    versions = relationship("ResumeVersion", back_populates="session", cascade="all, delete-orphan")
    interactions = relationship("AIInteraction", back_populates="session", cascade="all, delete-orphan")
    warnings = relationship("ValidationWarning", back_populates="session", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<ResumeSession {self.reference_code or self.id}>"


class ResumeSelectedProject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 17 — one project selected for this resume, with its overrides.

    A project carries no company or dates of its own — see ``app/models/project.py``
    — so there is nothing here to reattribute or to date; the overrides are
    purely how this project is framed for this JD.
    """

    __tablename__ = "resume_selected_projects"
    __table_args__ = (UniqueConstraint("session_id", "project_id", name="uq_session_project"),)

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null for a project composed inside the studio rather than picked from the
    #: bank. Section 17 allows the operator to build a project here directly;
    #: such a row carries its whole content in the overrides below and never
    #: writes back to the master pool.
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True)

    display_order = Column(Integer, nullable=False, default=0)
    is_primary = Column(String(10), nullable=True)

    #: Which selected employer this project prints under on this resume. The
    #: bank keeps projects and companies as independent pools, so this pairing
    #: exists only here, for this one composition — it is never written back to
    #: the master project. Held on the row rather than recomputed so a rebuild
    #: does not silently reshuffle which project sits under which employer.
    company_id = Column(GUID(), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)

    # Overrides — null means "use the master value".
    title_override = Column(String(255), nullable=True)
    role_override = Column(String(255), nullable=True)
    description_override = Column(Text, nullable=True)
    responsibilities_override = Column(StringList(), nullable=True)
    achievements_override = Column(StringList(), nullable=True)
    technologies_override = Column(StringList(), nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="selected_projects")
    project = relationship("Project")


class ResumeSelectedCompany(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 20 — companies shown in the experience section, in order."""

    __tablename__ = "resume_selected_companies"
    __table_args__ = (UniqueConstraint("session_id", "company_id", name="uq_session_company"),)

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id = Column(GUID(), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False)
    display_order = Column(Integer, nullable=False, default=0)
    designation_override = Column(String(255), nullable=True)
    start_date_override = Column(String(10), nullable=True)
    end_date_override = Column(String(10), nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="selected_companies")
    company = relationship("Company")


class ResumeSelectedTechnology(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_selected_technologies"
    __table_args__ = (UniqueConstraint("session_id", "technology_id", name="uq_session_technology"),)

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    technology_id = Column(GUID(), ForeignKey("technologies.id", ondelete="RESTRICT"), nullable=False)
    #: Skills-section grouping label, e.g. "Languages", "Cloud", "GenAI".
    section_label = Column(String(100), nullable=True)
    tier = Column(String(20), nullable=False, default="PRIMARY")
    display_order = Column(Integer, nullable=False, default=0)
    #: True when the technology comes from the JD but not the person's record.
    is_jd_driven = Column(String(10), nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="selected_technologies")
    technology = relationship("Technology")
