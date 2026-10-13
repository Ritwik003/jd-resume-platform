"""JD parsing and Requirement DTOs (sections 5, 14, 32)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.utils.enums import RequirementStatus, SkillImportance


class ParsedSkill(BaseModel):
    raw_label: str
    technology_id: UUID | None = None
    matched_name: str | None = None
    importance: SkillImportance = SkillImportance.MANDATORY


class ParsedJD(BaseModel):
    """Section 5 — AI extraction output, editable by the user before continuing."""

    role: str | None = None
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    location: str | None = None
    domain: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    other_requirements: list[str] = Field(default_factory=list)
    mandatory_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)


class JDParseRequest(BaseModel):
    raw_jd_text: str


class JDParsePreviewResponse(BaseModel):
    parsed: ParsedJD
    resolved_skills: list[ParsedSkill] = Field(default_factory=list)
    unmatched_skills: list[str] = Field(default_factory=list)
    model: str | None = None


class MissingTechnology(BaseModel):
    """A skill this JD asks for that the technology master has no row for.

    Carried to the generation form so the operator can add it before the
    resume is composed — a technology the bank has never heard of cannot be
    selected, cannot appear in the skills section, and cannot be checked
    against a period, so it is worth stopping for.
    """

    name: str
    importance: str = "MANDATORY"
    #: What the platform believes the release year is, for the operator to
    #: confirm or correct. Never guessed silently — it is shown and editable.
    suggested_release_year: int | None = None


class MissingTechnologiesResponse(BaseModel):
    requirement_id: UUID
    missing: list[MissingTechnology] = Field(default_factory=list)


class AddTechnologyRequest(BaseModel):
    """One technology the operator has approved adding to the master."""

    name: str
    #: Required. A technology with no release date silently opts out of every
    #: era check, which is the one thing these dates exist for.
    release_year: int = Field(ge=1960, le=2100)
    release_month: int = Field(default=1, ge=1, le=12)
    category: str | None = None


class AddTechnologiesRequest(BaseModel):
    technologies: list[AddTechnologyRequest] = Field(default_factory=list)


class RequirementCreate(BaseModel):
    """Step 1 of the workflow: the JD, the vendor and the template, together."""

    vendor_id: UUID
    raw_jd_text: str
    #: Chosen with the vendor at paste time; sessions inherit it.
    template_id: UUID | None = None
    #: Omit to have the JD parsed automatically on create.
    parsed: ParsedJD | None = None


class RequirementUpdate(BaseModel):
    """Section 5 — "The parsed information ... can be corrected before continuing." """

    parsed: ParsedJD | None = None
    #: Supplying new JD text re-runs the parser and rewrites the skill split,
    #: so correcting a typo updates this requirement instead of creating a
    #: second one for the same vacancy.
    raw_jd_text: str | None = None
    template_id: UUID | None = None
    status: RequirementStatus | None = None
    is_confirmed: bool | None = None


class RequirementTechnologyResponse(ORMModel):
    technology_id: UUID
    technology_name: str | None = None
    importance: str
    raw_label: str | None = None


class RequirementResponse(ORMModel):
    id: UUID
    reference_code: str | None = None
    vendor_id: UUID
    vendor_name: str | None = None
    template_id: UUID | None = None
    template_name: str | None = None
    raw_jd_text: str
    parsed_role: str | None = None
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    location: str | None = None
    domain: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    other_requirements: list[str] = Field(default_factory=list)
    status: str
    is_confirmed: str | None = None
    created_at: datetime
    technologies: list[RequirementTechnologyResponse] = Field(default_factory=list)
    #: The complete parser output exactly as stored, so the JD row is
    #: self-describing and a re-parse can be diffed against what came before.
    parsed_payload: dict | None = None
    parse_model: str | None = None
