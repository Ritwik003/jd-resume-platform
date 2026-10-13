"""Sections 10/11 — Person and PersonProfile.

"The employee identity should be stored only once. The profile types should be
separate records associated with the employee." One Person, many PersonProfiles.

No years-of-experience total is stored, on the person or on the profile. Every
resume is composed for one JD out of the shared bank, so the only experience
figure that means anything is the one that JD asks for — a stored career total
would be a second, competing answer that nothing reads and everything drifts
from. What a person really did is on the employment and project rows, with
their dates, which is where any span can be derived when one is needed.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, StringList

#: Section 11 — "Relevant project associations" per profile type.
person_profile_projects = Table(
    "person_profile_projects",
    Base.metadata,
    Column("person_profile_id", GUID(), ForeignKey("person_profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", GUID(), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
)

#: Section 11 — primary vs secondary skills, scoped to the profile type.
person_profile_technologies = Table(
    "person_profile_technologies",
    Base.metadata,
    Column("person_profile_id", GUID(), ForeignKey("person_profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("technology_id", GUID(), ForeignKey("technologies.id", ondelete="CASCADE"), primary_key=True),
    Column("tier", String(20), nullable=False, default="PRIMARY"),
    Column("display_order", String(10), nullable=True),
)


class ProfileType(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """AI Engineer, GenAI Engineer, Backend Engineer, Full Stack Engineer..."""

    __tablename__ = "profile_types"

    # No description. A profile type is a role NAME and nothing else: nothing
    # in composition, selection or validation ever read the column, so all it
    # did was ask an operator to write prose that no resume would ever show.
    name = Column(String(150), nullable=False, unique=True, index=True)

    profiles = relationship("PersonProfile", back_populates="profile_type")


class Person(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Section 11 — person-level identity, stored once."""

    __tablename__ = "persons"

    employee_code = Column(String(50), nullable=True, unique=True, index=True)
    full_name = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=True, unique=True, index=True)
    phone = Column(String(50), nullable=True)
    current_designation = Column(String(255), nullable=True)
    education = Column(Text, nullable=True)
    certifications = Column(StringList(), nullable=False, default=list)
    notes = Column(Text, nullable=True)

    profiles = relationship("PersonProfile", back_populates="person", cascade="all, delete-orphan")
    technologies = relationship("PersonTechnology", back_populates="person", cascade="all, delete-orphan")
    employments = relationship("Employment", back_populates="person", cascade="all, delete-orphan")
    vendor_statuses = relationship("ProfileVendorStatus", back_populates="person", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Person {self.full_name}>"


class PersonProfile(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A role-specific view of one person (section 10)."""

    __tablename__ = "person_profiles"
    __table_args__ = (UniqueConstraint("person_id", "profile_type_id", name="uq_person_profile_type"),)

    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_type_id = Column(GUID(), ForeignKey("profile_types.id", ondelete="RESTRICT"), nullable=False)
    title = Column(String(255), nullable=False)
    summary = Column(Text, nullable=True)
    headline_achievements = Column(StringList(), nullable=False, default=list)

    person = relationship("Person", back_populates="profiles")
    profile_type = relationship("ProfileType", back_populates="profiles")
    projects = relationship("Project", secondary=person_profile_projects, backref="person_profiles")
    technologies = relationship("Technology", secondary=person_profile_technologies)

    def __repr__(self) -> str:
        return f"<PersonProfile {self.title}>"


class Employment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """That a person worked somewhere — not when.

    No dates are stored here, or anywhere else on a person, company or
    project. A resume's timeline is not a record being reported; it is
    composed for one JD out of the years that JD asks for, laid out at
    generation time by ``ground_rules.meet_jd_experience``. Storing a second,
    competing set of dates only created something for the composed timeline to
    contradict — and every contradiction had to be explained to somebody.

    The bank supplies WHAT there is to say. When it happened is decided per
    resume, on the spot.
    """

    __tablename__ = "employments"

    person_id = Column(GUID(), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id = Column(GUID(), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False)
    designation = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)

    person = relationship("Person", back_populates="employments")
    company = relationship("Company")
