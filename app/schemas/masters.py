"""Master-data DTOs: companies, technologies, profile types, vendors, templates."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ------------------------------------------------------------------- company
class CompanyCreate(BaseModel):
    name: str
    #: The role this employer is offered under on a resume — matched against a
    #: JD's role independently of any project.
    role: str | None = None
    #: Section 16 — "Other metadata".
    metadata_json: dict | None = None


class CompanyUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    #: Section 16 — "Other metadata".
    metadata_json: dict | None = None
    is_active: bool | None = None


class CompanyResponse(ORMModel):
    id: UUID
    name: str
    role: str | None = None
    #: Section 16 — "Other metadata".
    metadata_json: dict | None = None
    is_active: bool


# ---------------------------------------------------------------- technology
class TechnologyCreate(BaseModel):
    name: str
    category: str | None = None
    sub_category: str | None = None
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    first_release_date: date | None = None


class TechnologyUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    sub_category: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    first_release_date: date | None = None
    is_active: bool | None = None


class TechnologyResponse(ORMModel):
    id: UUID
    name: str
    category: str | None = None
    sub_category: str | None = None
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    first_release_date: date | None = None
    is_active: bool


# -------------------------------------------------------------- profile type
class ProfileTypeCreate(BaseModel):
    name: str


class ProfileTypeResponse(ORMModel):
    id: UUID
    name: str
    is_active: bool


# -------------------------------------------------------------------- vendor
class VendorCreate(BaseModel):
    name: str
    contact_person: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None


class VendorUpdate(BaseModel):
    name: str | None = None
    contact_person: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class VendorResponse(ORMModel):
    id: UUID
    name: str
    contact_person: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None
    is_active: bool


# ------------------------------------------------------------------ template
class ResumeTemplateCreate(BaseModel):
    name: str
    code: str
    description: str | None = None
    profile_type_id: UUID | None = None
    layout_definition: dict = Field(default_factory=dict)


class ResumeTemplateResponse(ORMModel):
    id: UUID
    name: str
    code: str
    description: str | None = None
    profile_type_id: UUID | None = None
    layout_definition: dict
    version: str
    is_active: bool
    # Section 26 — how this template produces output.
    source_format: str = "LAYOUT"
    source_file_name: str | None = None
    source_size_bytes: int | None = None
    source_placeholders: list[str] | None = None
    #: True once the design has been converted into a fillable template, at
    #: which point every render is a deterministic fill rather than a rewrite.
    is_fillable: bool = False
    #: The design's own section headings, in its own order — what the content
    #: window for this template will show.
    section_titles: list[str] = Field(default_factory=list)
    main_tex_name: str | None = None
