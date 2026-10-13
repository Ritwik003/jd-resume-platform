"""/resume-sessions, /resume-generation, /ai-agent, /resume-versions, /validation.

Sections 19–27: the Resume Studio backend.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.ai_interaction import AIInteraction
from app.models.audit import AuditLog
from app.models.availability import ProfileReservation
from app.models.company import Company
from app.models.person import Person, PersonProfile
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume_session import (
    ResumeCompositionSession,
    ResumeSelectedCompany,
    ResumeSelectedProject,
    ResumeSelectedTechnology,
)
from app.models.resume_template import ResumeTemplate
from app.models.resume_version import ResumeVersion
from app.models.technology import Technology
from app.models.user import User
from app.models.validation_warning import ValidationWarning
from app.models.vendor import Vendor
from app.schemas.common import Page, PageMeta
from app.schemas.resume_content import ResumeContent
from app.schemas.sessions import (
    AIInteractionResponse,
    ApproveRequest,
    ApproveResponse,
    ChatRequest,
    ChatResponse,
    GenerateRequest,
    ManualEditRequest,
    ResumeFileResponse,
    ResumeVersionDetailResponse,
    ResumeVersionResponse,
    SectionEditRequest,
    SectionEditResponse,
    SelectedCompanyResponse,
    SelectedProjectResponse,
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    SessionSelectionUpdate,
    ValidationWarningResponse,
    VersionRestoreRequest,
    WarningDecisionRequest,
)
from app.services import progress
from app.services import resume_session_service as svc
from app.services.rendering import latex
from app.utils.exceptions import NotFoundError, ValidationError
from app.utils.pagination import Pagination, pagination_params

router = APIRouter()


def _detail(db: Session, session: ResumeCompositionSession) -> SessionDetailResponse:
    vendor = db.query(Vendor).filter(Vendor.id == session.vendor_id).first()
    person = db.query(Person).filter(Person.id == session.person_id).first()
    profile = db.query(PersonProfile).filter(PersonProfile.id == session.person_profile_id).first()
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()

    expires = None
    if session.reservation_id:
        res = db.query(ProfileReservation).filter(ProfileReservation.id == session.reservation_id).first()
        expires = res.expires_at if res else None

    selected: list[SelectedProjectResponse] = []
    for row in (
        db.query(ResumeSelectedProject)
        .filter(ResumeSelectedProject.session_id == session.id)
        .order_by(ResumeSelectedProject.display_order)
    ):
        project = (
            db.query(Project).filter(Project.id == row.project_id).first() if row.project_id else None
        )
        selected.append(
            SelectedProjectResponse(
                id=row.id,
                project_id=row.project_id,
                # A row with no bank project is one the operator composed here;
                # its title override is the only name it has.
                is_composed=row.project_id is None,
                project_name=(project.name if project else None) or row.title_override,
                display_order=row.display_order,
                is_primary=str(row.is_primary).strip().lower() in ("true", "1", "yes"),
                role_override=row.role_override,
                title_override=row.title_override,
                description_override=row.description_override,
                responsibilities_override=list(row.responsibilities_override or []) or None,
                achievements_override=list(row.achievements_override or []) or None,
                technologies_override=list(row.technologies_override or []) or None,
                company_id=row.company_id,
            )
        )

    tech_names = [
        t.name
        for _, t in db.query(ResumeSelectedTechnology, Technology)
        .join(Technology, Technology.id == ResumeSelectedTechnology.technology_id)
        .filter(ResumeSelectedTechnology.session_id == session.id)
        .order_by(ResumeSelectedTechnology.display_order)
        .all()
    ]
    company_rows = (
        db.query(ResumeSelectedCompany)
        .filter(ResumeSelectedCompany.session_id == session.id)
        .order_by(ResumeSelectedCompany.display_order)
        .all()
    )
    company_ids = [r.company_id for r in company_rows]
    companies: list[SelectedCompanyResponse] = []
    for row in company_rows:
        company = db.query(Company).filter(Company.id == row.company_id).first()
        companies.append(
            SelectedCompanyResponse(
                id=row.id,
                company_id=row.company_id,
                company_name=company.name if company else None,
                display_order=row.display_order,
                designation_override=row.designation_override,
                start_date_override=row.start_date_override,
                end_date_override=row.end_date_override,
            )
        )

    return SessionDetailResponse(
        **{
            **SessionDetailResponse.model_validate(session).model_dump(
                exclude={
                    "selected_projects",
                    "selected_companies",
                    "selected_company_ids",
                    "selected_technology_names",
                }
            ),
            "vendor_name": vendor.name if vendor else None,
            "person_name": person.full_name if person else None,
            "profile_title": profile.title if profile else None,
            "template_name": template.name if template else None,
            "requirement_code": requirement.reference_code if requirement else None,
            "reservation_expires_at": expires,
            "selected_projects": selected,
            "selected_companies": companies,
            "selected_company_ids": company_ids,
            "selected_technology_names": tech_names,
        }
    )


# ------------------------------------------------------------------- session
@router.post("", response_model=SessionDetailResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> SessionDetailResponse:
    """Section 20 — selecting a profile creates the session and reserves the person."""
    session = svc.create_session(
        db,
        requirement_id=payload.requirement_id,
        person_id=payload.person_id,
        person_profile_id=payload.person_profile_id,
        template_id=payload.template_id,
        reservation_id=payload.reservation_id,
        actor_id=user.id,
    )
    db.commit()
    return _detail(db, session)


@router.get("", response_model=Page[SessionResponse])
def list_sessions(
    vendor_id: UUID | None = Query(None),
    person_id: UUID | None = Query(None),
    requirement_id: UUID | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    reference_code: str | None = Query(None, description="Partial match on the session ref"),
    person: str | None = Query(None, description="Partial match on the person's name"),
    profile: str | None = Query(None, description="Partial match on the profile title"),
    vendor: str | None = Query(None, description="Partial match on the vendor's name"),
    template: str | None = Query(None, description="Partial match on the template name"),
    jd: str | None = Query(None, description="Partial match on the JD's reference code"),
    version_no: int | None = Query(None, description="Exact current version number"),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[SessionResponse]:
    """Sections 27/28 — enumerate composition sessions for history and resume.

    Deliberately lighter than ``GET /{session_id}``: the per-session selection
    lists are omitted so a long history does not fan out into N+1 queries.

    Every column the history table prints can be filtered here, including the
    four that are names on other tables. Doing it in the browser instead would
    filter only the page already fetched — which looks identical until the
    history outgrows one page, and then quietly starts lying. Filtering here
    also means ``total`` counts matches rather than rows, so the footer says
    something true.

    The joins are all many-to-one, so none of them fans a session into several
    rows; they are outer joins because a session may legitimately have no
    profile or no template yet.
    """
    query = db.query(ResumeCompositionSession)
    if vendor_id:
        query = query.filter(ResumeCompositionSession.vendor_id == vendor_id)
    if person_id:
        query = query.filter(ResumeCompositionSession.person_id == person_id)
    if requirement_id:
        query = query.filter(ResumeCompositionSession.requirement_id == requirement_id)
    if status_filter:
        query = query.filter(ResumeCompositionSession.status == status_filter)
    if reference_code:
        query = query.filter(ResumeCompositionSession.reference_code.ilike(f"%{reference_code}%"))
    if version_no is not None:
        query = query.filter(ResumeCompositionSession.current_version_no == version_no)
    if person:
        query = query.join(Person, Person.id == ResumeCompositionSession.person_id).filter(
            Person.full_name.ilike(f"%{person}%")
        )
    if profile:
        query = query.join(
            PersonProfile, PersonProfile.id == ResumeCompositionSession.person_profile_id
        ).filter(PersonProfile.title.ilike(f"%{profile}%"))
    if vendor:
        query = query.join(Vendor, Vendor.id == ResumeCompositionSession.vendor_id).filter(
            Vendor.name.ilike(f"%{vendor}%")
        )
    if template:
        query = query.join(
            ResumeTemplate, ResumeTemplate.id == ResumeCompositionSession.template_id
        ).filter(ResumeTemplate.name.ilike(f"%{template}%"))
    if jd:
        query = query.join(
            Requirement, Requirement.id == ResumeCompositionSession.requirement_id
        ).filter(Requirement.reference_code.ilike(f"%{jd}%"))

    total = query.count()
    rows = (
        query.order_by(ResumeCompositionSession.created_at.desc())
        .offset(page.skip)
        .limit(page.limit)
        .all()
    )

    # Resolve the display names in one pass per table rather than per row.
    vendors = {v.id: v.name for v in db.query(Vendor).all()}
    people = {p.id: p.full_name for p in db.query(Person).all()}
    profiles = {p.id: p.title for p in db.query(PersonProfile).all()}
    templates = {t.id: t.name for t in db.query(ResumeTemplate).all()}
    requirements = {r.id: r.reference_code for r in db.query(Requirement).all()}

    items = [
        SessionResponse(
            **{
                **SessionResponse.model_validate(row).model_dump(),
                "vendor_name": vendors.get(row.vendor_id),
                "person_name": people.get(row.person_id),
                "profile_title": profiles.get(row.person_profile_id),
                "template_name": templates.get(row.template_id),
                "requirement_code": requirements.get(row.requirement_id),
            }
        )
        for row in rows
    ]
    return Page(items=items, meta=PageMeta(skip=page.skip, limit=page.limit, total=total))


@router.get("/filter-options", response_model=dict)
def session_filter_options(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    """The values each history column can actually be filtered to.

    Derived from the sessions that exist, not from the master tables: offering a
    vendor nobody has ever composed for produces a dropdown where most choices
    return nothing, which is worse than a text box. Every option here matches at
    least one row.

    Declared above ``/{session_id}`` on purpose — FastAPI matches in definition
    order, and below it "filter-options" would be parsed as a session UUID.
    """
    rows = db.query(
        ResumeCompositionSession.reference_code,
        ResumeCompositionSession.person_id,
        ResumeCompositionSession.person_profile_id,
        ResumeCompositionSession.vendor_id,
        ResumeCompositionSession.template_id,
        ResumeCompositionSession.status,
        ResumeCompositionSession.current_version_no,
        ResumeCompositionSession.requirement_id,
    ).all()

    vendors = {v.id: v.name for v in db.query(Vendor).all()}
    people = {x.id: x.full_name for x in db.query(Person).all()}
    profiles = {x.id: x.title for x in db.query(PersonProfile).all()}
    templates = {t.id: t.name for t in db.query(ResumeTemplate).all()}
    requirements = {r.id: r.reference_code for r in db.query(Requirement).all()}

    def names(index: int, lookup: dict) -> list[str]:
        return sorted({lookup[r[index]] for r in rows if r[index] in lookup and lookup[r[index]]})

    return {
        # Newest first: a reference code is looked up, and the one somebody
        # wants is almost always recent.
        "reference_codes": sorted({r[0] for r in rows if r[0]}, reverse=True),
        "people": names(1, people),
        "profiles": names(2, profiles),
        "vendors": names(3, vendors),
        "templates": names(4, templates),
        "statuses": sorted({str(r[5]) for r in rows if r[5]}),
        "versions": sorted({r[6] for r in rows if r[6] is not None}),
        # A JD/requirement's own reference code — how one vendor+JD's sessions
        # tie back together in this table, newest first like reference codes.
        "jds": sorted(names(7, requirements), reverse=True),
    }


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return _detail(db, svc.get_session(db, session_id))


@router.put("/{session_id}/selections", response_model=SessionDetailResponse)
def update_selections(
    session_id: UUID,
    payload: SessionSelectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionDetailResponse:
    """Section 17 — the mix-and-match mapping. Master data is never modified."""
    session = svc.get_session(db, session_id)
    svc.update_selections(db, session, payload, actor_id=user.id)
    db.commit()
    return _detail(db, session)


@router.post("/{session_id}/rebuild", response_model=ResumeVersionDetailResponse)
def rebuild(session_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    session = svc.get_session(db, session_id)
    version, warnings = svc.rebuild_from_selections(db, session, actor_id=user.id)
    db.commit()
    return _version_detail(version, warnings)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    """Remove a draft session outright. Only while it is still EDITING —
    see ``resume_session_service.delete_session``; anything further along
    should be released (below) rather than erased."""
    session = svc.get_session(db, session_id)
    svc.delete_session(db, session, actor_id=user.id)
    db.commit()


@router.post("/{session_id}/abandon", response_model=SessionDetailResponse)
def abandon(session_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    session = svc.get_session(db, session_id)
    svc.abandon_session(db, session, actor_id=user.id)
    db.commit()
    return _detail(db, session)


@router.post("/{session_id}/reopen", response_model=SessionDetailResponse)
def reopen(session_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Take an approved resume back into the studio: RESERVED -> EDITING.

    The way out of a reservation that should not have been one. Approving again
    returns the session to RESERVED, so the loop runs as often as the work
    needs. A session somebody has already decided on is refused — see
    ``svc.reopen``.
    """
    session = svc.get_session(db, session_id)
    svc.reopen(db, session, actor_id=user.id)
    db.commit()
    return _detail(db, session)


# ---------------------------------------------------------------- generation
@router.get("/{session_id}/generation-progress")
def generation_progress(session_id: UUID, _: User = Depends(get_current_user)) -> dict:
    """The live checklist for a generation in flight (see app/services/progress.py).

    Read-only and cheap -- an in-memory lookup, no DB query -- so the studio
    can poll it every second or so while /generate is running elsewhere. Not
    tied to the session existing in the database: nothing has generated here
    yet reads the same as nothing to report, which is the right answer either
    way.
    """
    return progress.read(session_id)


@router.post("/{session_id}/generate", response_model=ResumeVersionDetailResponse)
def generate(
    session_id: UUID,
    payload: GenerateRequest = GenerateRequest(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ResumeVersionDetailResponse:
    """Section 19 — base resume generation."""
    session = svc.get_session(db, session_id)
    options = payload.options
    version, warnings, composition = svc.generate_base_resume(
        db,
        session,
        force=payload.force,
        actor_id=user.id,
        company_count=options.company_count if options else None,
        project_count=options.project_count if options else None,
        target_pages=options.target_pages if options else None,
    )
    db.commit()
    return _version_detail(version, warnings, composition)


@router.put("/{session_id}/content", response_model=ResumeVersionDetailResponse)
def manual_edit(
    session_id: UUID,
    payload: ManualEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ResumeVersionDetailResponse:
    """Section 23 — direct manual editing of generated content."""
    session = svc.get_session(db, session_id)
    version, warnings = svc.apply_manual_edit(
        db, session, payload.content, change_summary=payload.change_summary, actor_id=user.id
    )
    db.commit()
    return _version_detail(version, warnings)


# ------------------------------------------------------------ content window
@router.get("/{session_id}/sections")
def list_sections(
    session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict:
    """The content window for this session — the chosen design's own sections.

    Not a fixed set of fields: this is the section list of the template that
    was actually picked, in its order and its wording, each row saying how
    much content it currently holds. ``unused`` is the other half of the
    truth — content the resume carries that this design has nowhere to print,
    offered with the section that would give it a home.
    """
    session = svc.get_session(db, session_id)
    return svc.sections_view(db, session)


@router.post("/{session_id}/sections", response_model=SectionEditResponse)
def edit_sections(
    session_id: UUID,
    payload: SectionEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SectionEditResponse:
    """Add, remove, hide, rename, reorder or move a section.

    The same call the chat makes, so a click and a typed instruction produce
    the same version with the same summary. Every change is versioned, and the
    next render reads this list — so removing Education here removes it from
    the PDF, and adding Publications prints it through the design's own
    section macro rather than in some style the design never uses.
    """
    session = svc.get_session(db, session_id)
    version, _warnings, message = svc.edit_sections(
        db,
        session,
        payload.action,
        actor_id=user.id,
        section=payload.section,
        title=payload.title,
        kind=payload.kind,
        body=payload.body,
        items=payload.items,
        position=payload.position,
        order=payload.order,
        direction=payload.direction,
        purge=payload.purge,
    )
    db.commit()
    db.refresh(session)
    return SectionEditResponse(
        message=message,
        version_id=version.id,
        version_no=version.version_no,
        window=svc.sections_view(db, session),
    )


# -------------------------------------------------------------------- studio
@router.post("/{session_id}/chat", response_model=ChatResponse)
def chat(
    session_id: UUID,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatResponse:
    """Section 22 — the agentic chatbot turn."""
    session = svc.get_session(db, session_id)
    result = svc.chat_edit(db, session, payload.message, actor_id=user.id)
    db.commit()
    version = result["version"]
    return ChatResponse(
        reply=result["reply"],
        version_id=version.id if version else None,
        version_no=version.version_no if version else None,
        content=result["content"],
        tool_calls=result["tool_calls"],
        warnings=[ValidationWarningResponse.model_validate(w) for w in result["warnings"]],
    )


@router.get("/{session_id}/chat", response_model=list[AIInteractionResponse])
def chat_history(session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    svc.get_session(db, session_id)
    return (
        db.query(AIInteraction)
        .filter(AIInteraction.session_id == session_id)
        .order_by(AIInteraction.created_at)
        .all()
    )


@router.get("/{session_id}/preview", response_class=HTMLResponse)
def preview(session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> HTMLResponse:
    """Section 21 — the fast CONTENT preview.

    Renders from the layout definition, so it looks the same whichever template
    the session uses. Use ``/preview.pdf`` to see the chosen design.
    """
    session = svc.get_session(db, session_id)
    return HTMLResponse(svc.render_preview(db, session))


@router.get("/{session_id}/preview.pdf")
def preview_pdf(
    session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Response:
    """Section 21 — the resume as it will actually look.

    Compiles the current version through the session's own template. This is
    the only view that shows an uploaded design; the HTML preview cannot,
    because a .tex or .pdf template has no layout definition to render from.
    """
    session = svc.get_session(db, session_id)
    try:
        pdf = svc.render_design_pdf(db, session)
    except latex.LatexCompileError as exc:
        raise ValidationError(
            f"The template did not compile, so there is no design preview. {exc}"
        ) from exc
    db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="preview.pdf"'},
    )


# ------------------------------------------------------------------ versions
def _version_detail(
    version: ResumeVersion,
    warnings: list[ValidationWarning] | None = None,
    composition=None,
) -> ResumeVersionDetailResponse:
    return ResumeVersionDetailResponse(
        **{
            **ResumeVersionResponse.model_validate(version).model_dump(),
            "content": ResumeContent.model_validate(version.content_json),
            "warnings": [ValidationWarningResponse.model_validate(w) for w in (warnings or [])],
            "ground_rules": list(composition.ground_rules) if composition else [],
            "fit_score": composition.fit.score if composition else None,
            "fit_basis": composition.fit.basis if composition else None,
        }
    )


@router.get("/{session_id}/versions", response_model=list[ResumeVersionResponse])
def list_versions(session_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Section 27 — full version history, V1..Vn.

    Restores are events, not versions. Since restore_version repoints the
    session at an existing version rather than copying one forward, the only
    record that it happened is the audit trail — so it is read back here and
    attached to the version that was restored. The studio prints it as one
    quiet line, which is the whole point: "we came back to this one" is
    context, not a document worth its own row.
    """
    svc.get_session(db, session_id)
    versions = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.session_id == session_id)
        .order_by(ResumeVersion.version_no)
        .all()
    )
    by_id = {v.id: v for v in versions}
    restores = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "VERSION_RESTORED",
            AuditLog.entity_id.in_(list(by_id.keys()) or [None]),
        )
        .order_by(AuditLog.created_at)
        .all()
    )
    notes: dict[UUID, str] = {}
    # A version left behind by the Sections tab's inline Undo (see
    # ResumeVersionResponse.is_undone) rather than the general Versions-tab
    # Restore — the abandoned version is named by number, not id, in the
    # payload, since it may since have been restored back TO and so no longer
    # holds the id that recorded the restore.
    undone_version_nos: set[int] = set()
    for entry in restores:
        payload = entry.payload or {}
        came_from = payload.get("from_version_no")
        notes[entry.entity_id] = (
            f"Restored here from V{came_from}" if came_from else "Restored here"
        )
        if payload.get("undone") and came_from is not None:
            undone_version_nos.add(came_from)

    out = []
    for version in versions:
        row = ResumeVersionResponse.model_validate(version)
        row.restore_note = notes.get(version.id)
        row.is_undone = version.version_no in undone_version_nos
        out.append(row)
    return out


@router.get("/{session_id}/versions/{version_id}", response_model=ResumeVersionDetailResponse)
def get_version(
    session_id: UUID, version_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    version = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.session_id == session_id)
        .first()
    )
    if version is None:
        raise NotFoundError("Version not found in this session")
    stored = (
        db.query(ValidationWarning)
        .filter(ValidationWarning.version_id == version.id)
        .order_by(ValidationWarning.created_at.desc())
        .all()
    )
    return _version_detail(version, stored)


@router.post("/{session_id}/versions/{version_id}/restore", response_model=ResumeVersionDetailResponse)
def restore_version(
    session_id: UUID,
    version_id: UUID,
    payload: VersionRestoreRequest = VersionRestoreRequest(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Section 23 — "Reject AI changes" by restoring an earlier version."""
    session = svc.get_session(db, session_id)
    version = svc.restore_version(
        db,
        session,
        version_id,
        change_summary=payload.change_summary,
        actor_id=user.id,
        mark_undone=payload.mark_undone,
    )
    db.commit()
    return _version_detail(version)


@router.post("/{session_id}/versions/{version_id}/render", response_model=list[ResumeFileResponse])
def render_version(
    session_id: UUID,
    version_id: UUID,
    formats: list[str] = Query(default=["DOCX", "PDF"]),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    session = svc.get_session(db, session_id)
    version = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.session_id == session_id)
        .first()
    )
    if version is None:
        raise NotFoundError("Version not found in this session")
    files = svc.render_files(db, session, version, formats)
    db.commit()
    return [ResumeFileResponse.model_validate(f) for f in files]


# ---------------------------------------------------------------- validation
@router.get("/{session_id}/warnings", response_model=list[ValidationWarningResponse])
def list_warnings(
    session_id: UUID,
    only_open: bool = Query(False),
    all_versions: bool = Query(
        False, description="Include warnings raised against superseded versions."
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Sections 24/25 — warnings the user decides on; approval blocks on OPEN ones.

    Scoped to the current version by default. Validation re-runs on every
    version and writes a fresh row each time, so a finding that still applies at
    v4 exists four times — once per version. Returning all of them showed the
    same warning four times over with four sets of Keep/Dismiss buttons, and
    deciding one left the other three open.

    Nothing is lost by scoping: a finding that still applies was re-created
    against the current version, and one that no longer applies should not be
    asked about. ``all_versions`` returns the full history for auditing.
    """
    session = svc.get_session(db, session_id)
    query = db.query(ValidationWarning).filter(ValidationWarning.session_id == session_id)
    if not all_versions and session.current_version_id is not None:
        query = query.filter(ValidationWarning.version_id == session.current_version_id)
    if only_open:
        query = query.filter(ValidationWarning.status == "OPEN")
    return query.order_by(ValidationWarning.created_at.desc()).all()


@router.post("/{session_id}/warnings/{warning_id}/decision", response_model=ValidationWarningResponse)
def decide_warning(
    session_id: UUID,
    warning_id: UUID,
    payload: WarningDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Section 25 — the [Keep] / [Modify] decision, recorded against the user."""
    warning = (
        db.query(ValidationWarning)
        .filter(ValidationWarning.id == warning_id, ValidationWarning.session_id == session_id)
        .first()
    )
    if warning is None:
        raise NotFoundError("Warning not found in this session")
    svc.decide_warning(db, warning, payload.status, note=payload.note, actor_id=user.id)
    db.commit()
    return ValidationWarningResponse.model_validate(warning)


# ------------------------------------------------------------------- approval
@router.post("/{session_id}/approve", response_model=ApproveResponse)
def approve(
    session_id: UUID,
    payload: ApproveRequest = ApproveRequest(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApproveResponse:
    """Section 23 — final approval is always human-controlled."""
    session = svc.get_session(db, session_id)
    result = svc.approve(db, session, formats=payload.formats, notes=payload.notes, actor_id=user.id)
    db.commit()
    return ApproveResponse(
        session_id=session.id,
        version_id=result["version"].id,
        version_no=result["version"].version_no,
        submission_id=result["submission"].id,
        files=[ResumeFileResponse.model_validate(f) for f in result["files"]],
        open_warning_count=result["open_warning_count"],
    )
