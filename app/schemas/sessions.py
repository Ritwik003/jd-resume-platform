"""Resume session, studio, version, validation and submission DTOs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import FlexBool, ORMModel
from app.schemas.resume_content import ResumeContent
from app.utils.enums import (
    SubmissionStatus,
    ValidationWarningStatus,
)


# ------------------------------------------------------------------- session
class SessionCreate(BaseModel):
    requirement_id: UUID
    person_id: UUID
    #: The (person, role) row to compose against. Omit it — the usual case — and
    #: the role comes from the JD: selecting somebody for a "GenAI Engineer" JD
    #: is what makes them a GenAI Engineer candidate, so the row is created then
    #: rather than being typed in beforehand.
    person_profile_id: UUID | None = None
    template_id: UUID | None = None
    #: Reuse an existing reservation; one is created if omitted.
    reservation_id: UUID | None = None


class SelectedProjectPayload(BaseModel):
    """One row of the resume's project section.

    Section 17 mix-and-match, in one shape:

    * ``project_id`` set, no overrides  -> a bank project, shown as recorded
    * ``project_id`` set + overrides    -> a bank project, retitled/re-described
    * ``project_id`` null               -> a project composed in the studio; it
      lives only on this resume and never enters the master pool

    Every field except ``project_id`` is a per-resume override, so the same
    project can appear differently on two vendors' resumes without either copy
    touching the bank.
    """

    project_id: UUID | None = None
    display_order: int = 0
    is_primary: FlexBool = False
    title_override: str | None = None
    role_override: str | None = None
    description_override: str | None = None
    responsibilities_override: list[str] | None = None
    achievements_override: list[str] | None = None
    technologies_override: list[str] | None = None
    #: Which employer this project prints under on THIS resume. The pairing is
    #: a composition decision, not a fact about the project — the bank keeps
    #: projects and companies as independent pools — so it is edited here and
    #: stored on the selection row. Null lets the composer decide.
    company_id: UUID | None = None

    @model_validator(mode="after")
    def _composed_projects_need_a_title(self) -> SelectedProjectPayload:
        # A row with neither a bank project nor a title has nothing to render.
        if self.project_id is None and not (self.title_override or "").strip():
            raise ValueError(
                "A composed project needs a title. Either pick a project from the bank "
                "or give this one a name."
            )
        return self


class SelectedCompanyPayload(BaseModel):
    company_id: UUID
    display_order: int = 0
    designation_override: str | None = None
    start_date_override: str | None = None
    end_date_override: str | None = None


class SelectedTechnologyPayload(BaseModel):
    technology_id: UUID
    section_label: str | None = None
    tier: str = "PRIMARY"
    display_order: int = 0
    is_jd_driven: FlexBool = False


class SessionSelectionUpdate(BaseModel):
    """Section 23 — the user may change projects, companies and technologies."""

    projects: list[SelectedProjectPayload] | None = None
    companies: list[SelectedCompanyPayload] | None = None
    technologies: list[SelectedTechnologyPayload] | None = None


class SelectedProjectResponse(ORMModel):
    id: UUID
    #: Null when the project was composed in the studio rather than picked.
    project_id: UUID | None = None
    is_composed: bool = False
    project_name: str | None = None
    display_order: int
    #: The column stores "true"/"false" as text; the API speaks booleans so a
    #: client can echo this straight back into SelectedProjectPayload.
    is_primary: FlexBool = False
    role_override: str | None = None
    title_override: str | None = None
    description_override: str | None = None
    responsibilities_override: list[str] | None = None
    achievements_override: list[str] | None = None
    technologies_override: list[str] | None = None
    #: The employer this project sits under on this resume, so the Composition
    #: tab can group the page the way the resume prints it.
    company_id: UUID | None = None


class SelectedCompanyResponse(ORMModel):
    """An experience row, with the timeline the operator set for this resume."""

    id: UUID
    company_id: UUID
    company_name: str | None = None
    display_order: int
    designation_override: str | None = None
    start_date_override: str | None = None
    end_date_override: str | None = None


class SessionResponse(ORMModel):
    id: UUID
    reference_code: str | None = None
    requirement_id: UUID
    #: The JD's own reference code (REQ-00001, …) — one JD/vendor pair can
    #: have several sessions against it (one per candidate, or several drafts
    #: over time), and this is what ties them back together everywhere a
    #: session shows up: History, Vendor tracking, Reservations.
    requirement_code: str | None = None
    vendor_id: UUID
    vendor_name: str | None = None
    person_id: UUID
    person_name: str | None = None
    person_profile_id: UUID
    profile_title: str | None = None
    template_id: UUID
    template_name: str | None = None
    reservation_id: UUID | None = None
    reservation_expires_at: datetime | None = None
    status: str
    current_version_id: UUID | None = None
    current_version_no: int
    #: 0-100. At or above the configured threshold version 1 is the stored
    #: history transformed into the template; below it, the history was moulded
    #: toward the JD's role first.
    fit_score: int | None = None
    fit_basis: str | None = None
    created_at: datetime


class SessionDetailResponse(SessionResponse):
    selected_projects: list[SelectedProjectResponse] = Field(default_factory=list)
    selected_companies: list[SelectedCompanyResponse] = Field(default_factory=list)
    selected_company_ids: list[UUID] = Field(default_factory=list)
    selected_technology_names: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- generation
class GenerationOptions(BaseModel):
    """The shape the operator wants, asked before the first generation.

    Answered on the form that follows "Reserve & generate". Without these the
    platform picks its own counts and the operator spends the next ten minutes
    in the Composition tab undoing them — asking two questions up front is
    cheaper than a rebuild, and it lets the selection, the moulding and the
    timeline all be decided against the same numbers.
    """

    #: How many employers the experience section carries.
    company_count: int | None = Field(default=None, ge=1, le=8)
    #: How many projects the resume carries IN TOTAL. Deliberately not a
    #: per-company figure: how the projects divide between employers is the
    #: composer's call, and an even split is rarely the best story. Four
    #: companies and six projects might land 2/2/1/1 or 3/2/1/0 depending on
    #: which employer the JD-relevant work belongs under.
    project_count: int | None = Field(default=None, ge=1, le=12)
    #: Pages the finished resume should fill.
    target_pages: int | None = Field(default=None, ge=1, le=3)


class GenerateRequest(BaseModel):
    """Section 19 — base resume generation."""

    #: Regenerate from scratch even when a version already exists.
    force: bool = False
    instructions: str | None = None
    #: The counts the operator chose on the generation form. Absent means
    #: "use the house defaults" — the API stays usable without the form.
    options: GenerationOptions | None = None


class ManualEditRequest(BaseModel):
    """Section 23 — "Directly edit generated content"."""

    content: ResumeContent
    change_summary: str | None = None


# -------------------------------------------------------------------- studio
class ChatRequest(BaseModel):
    """Section 22 — natural-language instruction to the agentic chatbot."""

    message: str


class ToolCallRecord(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)
    result: str | None = None
    applied: bool = True


class SectionEditRequest(BaseModel):
    """One change to the content window.

    Deliberately one shape for every operation rather than seven endpoints:
    the studio's section list does all of these from the same row, and a
    single payload keeps the client from learning seven of them.
    """

    #: add | remove | show | hide | rename | reorder | move
    action: str
    #: Which section, for everything except add and reorder. The design's own
    #: heading, its key, or the generic name — all three resolve.
    section: str | None = None
    #: The heading, for add and rename.
    title: str | None = None
    #: summary|skills|experience|projects|education|certifications|custom.
    #: Inferred from the title when omitted.
    kind: str | None = None
    body: str = ""
    items: list[str] = Field(default_factory=list)
    #: Where to insert, for add. Appended when omitted.
    position: int | None = None
    #: The new top-to-bottom order, for reorder.
    order: list[str] = Field(default_factory=list)
    #: up | down, for move.
    direction: str = "up"
    #: Drop the section outright rather than hiding it. Only for one the
    #: operator added here — a section of the design is hidden, so it can come
    #: back with its content intact.
    purge: bool = False


class SectionEditResponse(BaseModel):
    """The change that was made, plus the window as it now stands."""

    message: str
    version_id: UUID
    version_no: int
    window: dict


class ChatResponse(BaseModel):
    reply: str
    version_id: UUID | None = None
    version_no: int | None = None
    content: ResumeContent | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    warnings: list[ValidationWarningResponse] = Field(default_factory=list)


class AIInteractionResponse(ORMModel):
    id: UUID
    session_id: UUID
    role: str
    message: str | None = None
    tool_calls: list | dict | None = None
    resulting_version_id: UUID | None = None
    created_at: datetime


# ------------------------------------------------------------------- version
class ResumeFileResponse(ORMModel):
    id: UUID
    file_format: str
    file_name: str
    size_bytes: int | None = None


class ResumeVersionResponse(ORMModel):
    id: UUID
    session_id: UUID
    version_no: int
    change_summary: str | None = None
    change_origin: str | None = None
    status: str
    created_at: datetime
    files: list[ResumeFileResponse] = Field(default_factory=list)
    #: Set when the session was restored back to this version. A restore
    #: repoints rather than creating a version (see
    #: resume_session_service.restore_version), so this note is the only trace
    #: of it in the history — printed as one quiet line under the row.
    restore_note: str | None = None
    #: Set when this version was the one left behind by an explicit "Undo" of
    #: a section change, rather than by a general Versions-tab restore. An
    #: ordinary restore leaves the abandoned version restorable again — that
    #: is the whole point of keeping history — but Undo is meant to actually
    #: retire the mistake it is undoing, so the studio hides Restore for it.
    is_undone: bool = False


class ResumeVersionDetailResponse(ResumeVersionResponse):
    content: ResumeContent
    # Sections 24/25: every mutation that can introduce a claim reports the
    # warnings it produced, so the studio never has to poll for them.
    warnings: list[ValidationWarningResponse] = Field(default_factory=list)
    #: House rules applied to this version (education dropped, timeline
    #: expanded, ...). Shown in the studio so the operator can see what the
    #: platform changed on their behalf.
    ground_rules: list[str] = Field(default_factory=list)
    fit_score: int | None = None
    fit_basis: str | None = None


class VersionRestoreRequest(BaseModel):
    """Section 23 — "Reject AI changes" / "Request another version"."""

    change_summary: str | None = None
    #: Set by the Sections tab's inline Undo, never by the general Versions
    #: tab. Marks the version being left behind as retired rather than merely
    #: superseded, so it stops offering itself as something to restore back
    #: to — see ResumeVersionResponse.is_undone.
    mark_undone: bool = False


# ---------------------------------------------------------------- validation
class ValidationWarningResponse(ORMModel):
    id: UUID
    session_id: UUID
    version_id: UUID | None = None
    warning_type: str
    severity: str
    message: str
    content_path: str | None = None
    claim_text: str | None = None
    evidence: dict | None = None
    status: str
    decided_at: datetime | None = None
    decision_note: str | None = None


class WarningDecisionRequest(BaseModel):
    """Section 25 — [Modify] / [Keep]. The platform never hard-blocks."""

    status: ValidationWarningStatus
    note: str | None = None


# ------------------------------------------------------------------- approval
class ApproveRequest(BaseModel):
    """Section 23 — final approval is always human-controlled."""

    notes: str | None = None
    #: Formats to render on approval.
    formats: list[str] = Field(default_factory=lambda: ["DOCX", "PDF"])


class ApproveResponse(BaseModel):
    session_id: UUID
    version_id: UUID
    version_no: int
    submission_id: UUID
    files: list[ResumeFileResponse]
    open_warning_count: int


# ---------------------------------------------------------------- submission
class SubmissionResponse(ORMModel):
    id: UUID
    reference_code: str | None = None
    session_id: UUID
    version_id: UUID
    version_no: int | None = None
    person_id: UUID
    person_name: str | None = None
    vendor_id: UUID
    vendor_name: str | None = None
    requirement_id: UUID
    status: str
    approved_at: datetime | None = None
    submitted_at: datetime | None = None
    created_at: datetime


class SubmissionStatusUpdate(BaseModel):
    status: SubmissionStatus
    reason: str | None = None


# ValidationWarningResponse is declared below the version models.
ResumeVersionDetailResponse.model_rebuild()
