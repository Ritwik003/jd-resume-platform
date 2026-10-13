"""Person, PersonProfile, PersonTechnology, Project DTOs (sections 10–15)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import FlexBool, ORMModel
from app.utils.enums import ProficiencyLevel, TechnologyTier


# -------------------------------------------------------------------- person
class PersonCreate(BaseModel):
    full_name: str
    employee_code: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    current_designation: str | None = None
    education: str | None = None
    certifications: list[str] = Field(default_factory=list)
    notes: str | None = None


class PersonUpdate(BaseModel):
    full_name: str | None = None
    employee_code: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    current_designation: str | None = None
    education: str | None = None
    certifications: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class PersonResponse(ORMModel):
    id: UUID
    employee_code: str | None = None
    full_name: str
    email: str | None = None
    phone: str | None = None
    current_designation: str | None = None
    education: str | None = None
    certifications: list[str] = Field(default_factory=list)
    #: Section 11 — "Other general attributes".
    notes: str | None = None
    is_active: bool


# ------------------------------------------------------------ person profile
class PersonProfileCreate(BaseModel):
    profile_type_id: UUID
    title: str
    summary: str | None = None
    headline_achievements: list[str] = Field(default_factory=list)
    project_ids: list[UUID] = Field(default_factory=list)


class PersonProfileUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    headline_achievements: list[str] | None = None
    project_ids: list[UUID] | None = None
    is_active: bool | None = None


class PersonProfileResponse(ORMModel):
    id: UUID
    person_id: UUID
    profile_type_id: UUID
    title: str
    summary: str | None = None
    headline_achievements: list[str] = Field(default_factory=list)
    is_active: bool


# --------------------------------------------------------- person technology
class PersonTechnologyCreate(BaseModel):
    technology_id: UUID
    years_of_experience: float | None = None
    proficiency: ProficiencyLevel | None = None
    is_primary: FlexBool = False
    notes: str | None = None


class PersonTechnologyResponse(ORMModel):
    id: UUID
    person_id: UUID
    technology_id: UUID
    technology_name: str | None = None
    years_of_experience: float | None = None
    proficiency: str | None = None
    #: Stored as text on the column; the API speaks booleans so a client can
    #: echo it back without a type dance.
    is_primary: FlexBool = False
    #: Where the numbers came from — "assessed — 3 projects, 2023-2026" or a
    #: human's own note. Section 13 values are inferred, so their basis travels
    #: with them rather than being lost.
    notes: str | None = None


# -------------------------------------------------------------- profile tech
class ProfileTechnologyLink(BaseModel):
    technology_id: UUID
    tier: TechnologyTier = TechnologyTier.PRIMARY


# ------------------------------------------------------------------- project
class ProjectTechnologyLink(BaseModel):
    technology_id: UUID
    is_core: FlexBool = True


class ProjectCreate(BaseModel):
    name: str
    role: str | None = None
    short_description: str | None = None
    detailed_description: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    technologies: list[ProjectTechnologyLink] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    short_description: str | None = None
    detailed_description: str | None = None
    responsibilities: list[str] | None = None
    keywords: list[str] | None = None
    technologies: list[ProjectTechnologyLink] | None = None
    is_active: bool | None = None


class ProjectResponse(ORMModel):
    id: UUID
    name: str
    role: str | None = None
    short_description: str | None = None
    detailed_description: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    technology_names: list[str] = Field(default_factory=list)
    is_active: bool


# ---------------------------------------------------------------- employment
class EmploymentCreate(BaseModel):
    person_id: UUID
    company_id: UUID
    designation: str | None = None
    location: str | None = None


class EmploymentResponse(ORMModel):
    id: UUID
    person_id: UUID
    company_id: UUID
    company_name: str | None = None
    designation: str | None = None
    location: str | None = None
