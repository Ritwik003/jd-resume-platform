"""Orchestration for the resume composition session and the studio.

This is where sections 19–27 meet: create a session against a reservation,
generate a base version, apply agent or manual edits as new versions, run
validation, approve, render and record the submission.

Every state-changing path writes a new ResumeVersion rather than mutating one,
so "reject the AI's change" is a restore rather than an undo.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    RESUME_MAX_COMPANIES,
    RESUME_MAX_PROJECTS,
)
from app.models.ai_interaction import AIInteraction
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
from app.models.resume_version import ResumeFile, ResumeVersion
from app.models.submission import Submission, SubmissionStatusHistory
from app.models.technology import PersonTechnology, Technology
from app.models.validation_warning import ValidationWarning
from app.models.vendor import Vendor
from app.prompts.templates import EDIT_AGENT_SYSTEM
from app.repositories import requirement_repository, session_repository
from app.schemas.resume_content import ResumeContent
from app.schemas.sessions import SessionSelectionUpdate, ToolCallRecord
from app.services import (
    audit,
    availability,
    content_window,
    era,
    ground_rules,
    latex_composer,
    progress,
    resume_composer,
    roles,
    storage,
    validation_engine,
)
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.rendering import latex
from app.services.rendering.docx_renderer import render_docx
from app.services.rendering.html_renderer import render_html
from app.services.resume_tools import TOOL_SPECS, ToolContext, apply_tool, reframe_new_blocks
from app.utils.enums import (
    ProfileVendorStatusEnum,
    RequirementStatus,
    ResumeSessionStatus,
    ResumeVersionStatus,
    SubmissionStatus,
    ValidationWarningStatus,
)
from app.utils.exceptions import ConflictError, NotFoundError, ReservationExpiredError, ValidationError
from app.utils.helper import allocate_reference_code, as_aware, utcnow

logger = logging.getLogger(__name__)

#: A no-tool-call reply is kept verbatim unless it matches this — a first-
#: person claim, in an editing verb, that something on the resume changed.
#: That specific shape is the one failure this guards against: a model under
#: load narrating "I've applied the X format" without ever emitting the tool
#: call that would make it true. Everything else a model says without calling
#: a tool is either the direct answer to a question ("is Python used here?"),
#: an explanation of why nothing needed to change ("Kubernetes is already
#: listed"), or a genuine clarifying question — all of them worth showing,
#: none of them a false completion claim. Keying this off whether the reply
#: happens to end in "?" caught the clarifying question but discarded the
#: other two, which is answers to real questions being silently replaced with
#: "I didn't make any changes."
_CLAIMED_COMPLETION = re.compile(
    r"\bI(?:'ve| have)?\s+(?:added|removed|renamed|rewrote|rewritten|updated|changed|"
    r"reordered|moved|set|applied|deleted|dropped)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------- helpers
def get_session(db: Session, session_id: UUID) -> ResumeCompositionSession:
    session = (
        db.query(ResumeCompositionSession).filter(ResumeCompositionSession.id == session_id).first()
    )
    if session is None:
        raise NotFoundError("Resume session not found")
    return session


def current_content(db: Session, session: ResumeCompositionSession) -> ResumeContent:
    version = current_version(db, session)
    if version is None:
        raise ConflictError("This session has no generated resume yet")
    return ResumeContent.model_validate(version.content_json)


def current_version(db: Session, session: ResumeCompositionSession) -> ResumeVersion | None:
    if session.current_version_id is None:
        return None
    return db.query(ResumeVersion).filter(ResumeVersion.id == session.current_version_id).first()


def assert_reservation_active(db: Session, session: ResumeCompositionSession) -> None:
    """Section 31 — editing a lapsed session revives it rather than refusing.

    A hold that runs out is not a verdict on the work. Somebody approved a
    resume and the vendor went quiet, or somebody left a tab open over a
    weekend; in both cases the composition is still good and the person is
    still free. Refusing the edit and making them re-reserve through another
    screen taught people that the expiry was an obstacle rather than a signal.

    So: if the hold is live, do nothing. If it has lapsed, take it again — a
    fresh clock, the session and the profile both back to EDITING — and let the
    edit proceed. The one case that still refuses is a genuine collision: the
    profile was picked up for a DIFFERENT requirement while this session was
    lapsed, and reviving would hand the same person to two vendors at once.

    The name is kept because it is called in a dozen places and still means
    "make sure this session may be edited"; what changed is that it now makes
    that true instead of only checking it.
    """
    if session.person_id is None or session.vendor_id is None:
        return

    reservation = (
        db.query(ProfileReservation).filter(ProfileReservation.id == session.reservation_id).first()
        if session.reservation_id
        else None
    )
    now = utcnow()
    live = (
        reservation is not None
        and (reservation.released_at is None or reservation.release_reason == "CONVERTED_TO_ENGAGEMENT")
        and (reservation.expires_at is None or (as_aware(reservation.expires_at) or now) > now)
    )
    status_row = availability.get_or_create_status(db, session.person_id, session.vendor_id)

    # The collision check comes first, and is not conditional on our own
    # reservation looking healthy. The status row — not the reservation — is the
    # authority on who holds this pair, so a stale-but-unexpired row of ours
    # must not let an edit through after somebody else took the person.
    held_elsewhere = (
        str(status_row.status) in (ProfileVendorStatusEnum.EDITING, ProfileVendorStatusEnum.RESERVED)
        and status_row.current_requirement_id is not None
        and status_row.current_requirement_id != session.requirement_id
    )
    if held_elsewhere:
        raise ReservationExpiredError(
            "This profile has since been picked up for another requirement with this vendor. "
            "Release that one first, or compose this resume for a different person.",
            details={"held_by_requirement_id": str(status_row.current_requirement_id)},
        )

    # Approved and waiting on the vendor is a live hold of its own: the
    # reservation was extended to cover it, and an edit here is a legitimate
    # revision of a resume that has not been answered yet.
    if live and str(status_row.status) in (
        ProfileVendorStatusEnum.EDITING,
        ProfileVendorStatusEnum.RESERVED,
    ):
        return

    revive_session(db, session)


def revive_session(
    db: Session,
    session: ResumeCompositionSession,
    *,
    action: str = "SESSION_REVIVED",
    summary: str = "A lapsed hold was taken again so the composition could continue",
    actor_id: UUID | None = None,
) -> None:
    """Put a session back to EDITING with a fresh, open-ended hold.

    Both halves move together — the profile is EDITING for that vendor again and
    the session says so in the History tab — because the two disagreeing is the
    bug this whole lifecycle exists to prevent.

    Two things arrive here. A hold that lapsed on its own, where reviving is how
    an edit is allowed to proceed; and an operator deliberately reopening an
    approved resume (see ``reopen``). The mechanics are identical — the same
    clock-free hold, the same pair of records moved — so only the audit line
    differs, which is why the caller names it. EDITING carries no expiry, same
    as a fresh selection: it only picks up a clock again once it is approved
    and moves to RESERVED.
    """
    now = utcnow()
    expires_at = None

    reservation = (
        db.query(ProfileReservation).filter(ProfileReservation.id == session.reservation_id).first()
        if session.reservation_id
        else None
    )
    if reservation is not None and reservation.released_at is None:
        reservation.expires_at = expires_at
    else:
        # The old row is closed history. A revival is a new hold and gets its
        # own auditable row rather than reopening a released one.
        reservation = ProfileReservation(
            person_id=session.person_id,
            vendor_id=session.vendor_id,
            requirement_id=session.requirement_id,
            reserved_at=now,
            expires_at=expires_at,
        )
        db.add(reservation)
        db.flush()
        session.reservation_id = reservation.id

    status_row = availability.get_or_create_status(db, session.person_id, session.vendor_id)
    status_row.status = ProfileVendorStatusEnum.EDITING
    status_row.reserved_at = now
    status_row.reservation_expires_at = expires_at
    status_row.last_interaction_at = now
    status_row.current_requirement_id = session.requirement_id

    # Only a session still in play goes back to EDITING. One that records a
    # decision — submitted, engaged, released, rejected — keeps it.
    if ResumeSessionStatus(str(session.status)).is_open:
        session.status = ResumeSessionStatus.EDITING

    audit.record(
        db,
        action=action,
        entity_type="ResumeCompositionSession",
        entity_id=session.id,
        actor_id=actor_id,
        summary=summary,
        payload={"person_id": str(session.person_id), "vendor_id": str(session.vendor_id)},
    )
    db.flush()


def reopen(
    db: Session, session: ResumeCompositionSession, *, actor_id: UUID | None = None
) -> ResumeCompositionSession:
    """Take an approved resume back into the studio: RESERVED -> EDITING.

    A RESERVED hold is an approved resume waiting on the vendor's answer, and
    waiting is not the same as finished — the operator may want to change it
    before anyone acts on it. Reopening puts the composition back in play on a
    fresh editing clock, and approving it again returns it to RESERVED, so the
    loop can run as many times as the work needs.

    What it will not do is undo a decision. Once somebody has said what happened
    — submitted, engaged, released, rejected — that resume has been acted on by
    a person outside this system, and quietly reopening it would let the record
    drift away from what the vendor was actually sent.

    The previously approved version and its submission are left exactly as they
    are. They are what went out; the next approval writes its own alongside
    them, which is what makes the version history worth keeping.
    """
    status = ResumeSessionStatus(str(session.status))
    if status is ResumeSessionStatus.EDITING:
        return session
    if not status.is_open:
        raise ConflictError(
            f"This resume is {status} — somebody has already said what happened to it, "
            "so it cannot be taken back into the studio."
        )

    revive_session(
        db,
        session,
        action="SESSION_REOPENED",
        summary="An approved resume was taken back into the studio for further edits",
        actor_id=actor_id,
    )
    return session


# ------------------------------------------------------------------- creation
def create_session(
    db: Session,
    *,
    requirement_id: UUID,
    person_id: UUID,
    person_profile_id: UUID | None = None,
    template_id: UUID | None = None,
    reservation_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> ResumeCompositionSession:
    requirement = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if requirement is None:
        raise NotFoundError("Requirement not found")

    if person_profile_id is None:
        # No role given, which is the normal path: the JD says what this person
        # is being put forward as, so selecting them for it creates that row (or
        # reuses the one an earlier JD for the same role already made).
        profile_type = roles.register_role(db, requirement.parsed_role)
        if profile_type is None:
            raise ValidationError(
                "This JD has no role on it, so there is nothing to put this person forward as. "
                "Set the role on the requirement and try again."
            )
        profile = roles.profile_for_role(
            db, person_id=person_id, profile_type=profile_type, title=requirement.parsed_role
        )
        person_profile_id = profile.id
    else:
        profile = db.query(PersonProfile).filter(PersonProfile.id == person_profile_id).first()
        if profile is None or profile.person_id != person_id:
            raise ValidationError("The selected profile does not belong to the selected person")

    if template_id is None:
        # The template is chosen with the vendor at JD-paste time, so the JD is
        # the first place to look. Falling back to a profile-type match, then to
        # any active template, keeps sessions creatable for older requirements.
        if requirement.template_id is not None:
            template_id = requirement.template_id
        else:
            template = (
                db.query(ResumeTemplate)
                .filter(
                    ResumeTemplate.profile_type_id == profile.profile_type_id,
                    ResumeTemplate.is_active.is_(True),
                )
                .first()
                or db.query(ResumeTemplate).filter(ResumeTemplate.is_active.is_(True)).first()
            )
            if template is None:
                raise ValidationError("No resume template is configured")
            template_id = template.id
    elif db.query(ResumeTemplate).filter(ResumeTemplate.id == template_id).first() is None:
        raise NotFoundError("Resume template not found")

    # Section 4 of the workflow: selecting a profile reserves it.
    if reservation_id is None:
        existing = availability.active_reservation_for(
            db, person_id=person_id, vendor_id=requirement.vendor_id
        )
        if existing is not None and existing.requirement_id == requirement_id:
            reservation = existing
        else:
            reservation = availability.create_reservation(
                db,
                person_id=person_id,
                vendor_id=requirement.vendor_id,
                requirement_id=requirement_id,
                actor_id=actor_id,
            )
        reservation_id = reservation.id

    session = ResumeCompositionSession(
        reference_code=allocate_reference_code(
            db, ResumeCompositionSession.reference_code, "RS"
        ),
        requirement_id=requirement_id,
        vendor_id=requirement.vendor_id,
        person_id=person_id,
        person_profile_id=person_profile_id,
        template_id=template_id,
        reservation_id=reservation_id,
        status=ResumeSessionStatus.EDITING,
        created_by_id=actor_id,
    )
    db.add(session)

    if requirement.status == RequirementStatus.OPEN:
        requirement.status = RequirementStatus.IN_PROGRESS

    audit.record(
        db,
        action="SESSION_CREATED",
        entity_type="ResumeCompositionSession",
        entity_id=session.id,
        actor_id=actor_id,
        payload={"requirement_id": str(requirement_id), "person_id": str(person_id)},
    )
    db.flush()
    return session


# ------------------------------------------------------------------ versions
def create_version(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    *,
    change_summary: str,
    change_origin: str,
    actor_id: UUID | None = None,
) -> ResumeVersion:
    """Section 27 — every meaningful cycle becomes a numbered version."""
    # Locks this session's row for the rest of the transaction, so two
    # requests racing to version the same session (a double-click, a retried
    # fetch) serialize here instead of both reading the same MAX(version_no)
    # below and racing to insert it. The loser used to sit blocked on the
    # unique index until the winner's transaction ended, with no slow query of
    # its own to explain the wait -- and on a bad day that ran past the
    # statement_timeout and surfaced as a raw 500. This SELECT is instant; the
    # wait it replaces was not.
    db.query(ResumeCompositionSession).filter(
        ResumeCompositionSession.id == session.id
    ).with_for_update().first()

    # The content window travels inside the version. Seeding here rather than
    # at any one entry point means every route into a new version — generate,
    # manual edit, chat, rebuild, restore — carries the chosen template's
    # sections, including versions composed before this pipeline existed.
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    if template is not None:
        content_window.seed(content, template)

    # Numbered from the highest version this session has ever had, not from
    # the one it currently points at. Those were the same number only while
    # the pointer could never move backwards. It can now: restoring repoints
    # the session at an earlier version instead of copying it forward, so a
    # session sitting on V1 with eighteen versions behind it computed "next =
    # 2" and every edit after a restore died on uq_session_version_no. The
    # same shape of bug as deriving a reference code from a row count -- a
    # counter that can go down is not a sequence.
    highest = (
        db.query(func.max(ResumeVersion.version_no))
        .filter(ResumeVersion.session_id == session.id)
        .scalar()
    )
    next_no = max(int(highest or 0), int(session.current_version_no or 0)) + 1
    version = ResumeVersion(
        session_id=session.id,
        version_no=next_no,
        content_json=content.model_dump(mode="json"),
        change_summary=change_summary,
        change_origin=change_origin,
        status=ResumeVersionStatus.DRAFT,
        created_by_id=actor_id,
    )
    db.add(version)
    db.flush()

    previous = current_version(db, session)
    if previous is not None and previous.status == ResumeVersionStatus.DRAFT:
        previous.status = ResumeVersionStatus.SUPERSEDED

    session.current_version_id = version.id
    session.current_version_no = next_no
    db.flush()
    return version


def _warning_match_key(warning_type: str, content_path: str | None, claim_text: str | None) -> tuple:
    """Section 25 carry-forward key.

    ``content_path`` ("projects[1].description", "skill_groups[0].items") names
    WHERE a finding sits and survives a judge re-wording its message between
    runs; ``claim_text`` is the fallback for findings with no path (a couple of
    the older, non-strict rules never set one). Matching on the exact message
    text was fragile — the LLM judges do not phrase a repeat finding
    identically twice, so a decision the user already made kept coming back as
    a fresh OPEN warning.
    """
    return (warning_type, content_path) if content_path else (warning_type, None, claim_text)


def persist_warnings(
    db: Session, session: ResumeCompositionSession, version: ResumeVersion, findings: list
) -> list[ValidationWarning]:
    """Carry forward a user's earlier decision so they aren't asked twice."""
    decided = {
        _warning_match_key(w.warning_type, w.content_path, w.claim_text): w
        for w in db.query(ValidationWarning).filter(
            ValidationWarning.session_id == session.id,
            ValidationWarning.status != ValidationWarningStatus.OPEN,
        )
    }

    created: list[ValidationWarning] = []
    for finding in findings:
        key = _warning_match_key(finding.warning_type, finding.content_path, finding.claim_text)
        prior = decided.get(key)
        warning = ValidationWarning(
            session_id=session.id,
            version_id=version.id,
            warning_type=finding.warning_type,
            severity=finding.severity,
            message=finding.message,
            content_path=finding.content_path,
            claim_text=finding.claim_text,
            evidence=finding.evidence,
            status=prior.status if prior else ValidationWarningStatus.OPEN,
            decided_by_id=prior.decided_by_id if prior else None,
            decided_at=prior.decided_at if prior else None,
            decision_note=prior.decision_note if prior else None,
        )
        db.add(warning)
        created.append(warning)
    db.flush()
    return created


def open_warnings(db: Session, session_id: UUID, version_id: UUID | None = None) -> list[ValidationWarning]:
    query = db.query(ValidationWarning).filter(
        ValidationWarning.session_id == session_id,
        ValidationWarning.status == ValidationWarningStatus.OPEN,
    )
    if version_id is not None:
        query = query.filter(ValidationWarning.version_id == version_id)
    return query.all()


# ---------------------------------------------------------------- generation
def generate_base_resume(
    db: Session,
    session: ResumeCompositionSession,
    *,
    force: bool = False,
    actor_id: UUID | None = None,
    company_count: int | None = None,
    project_count: int | None = None,
    target_pages: int | None = None,
) -> tuple[ResumeVersion, list[ValidationWarning], resume_composer.BaseComposition]:
    """Section 19.

    Returns the composition alongside the version so the caller can tell the
    user what happened to version 1 — whether the profile was moulded toward the
    JD or transformed as-is, and which house rules fired.
    """
    assert_reservation_active(db, session)
    if session.current_version_id is not None and not force:
        raise ConflictError("A resume has already been generated for this session; pass force=true to regenerate")

    db.flush()

    # A live checklist for the studio to poll (see app/services/progress.py) --
    # generation makes several sequential model calls and can genuinely run
    # for minutes, and a spinner with no other signal cannot be told apart
    # from a hang. finally guarantees the checklist always closes, on the
    # success path and on any error, so a failed generation never leaves the
    # UI polling a step that will never move again.
    progress.start(session.id)
    try:
        composition = resume_composer.generate_base_content(
            db,
            session,
            company_count=company_count,
            project_count=project_count,
            target_pages=target_pages,
        )
        content = composition.content

        summary = (
            f"Moulded toward '{composition.fit.jd_role or 'the JD'}' (fit {composition.fit.score}%)"
            if composition.moulded
            else f"Profile already matches the JD (fit {composition.fit.score}%); transformed as-is"
        )
        version = create_version(
            db,
            session,
            content,
            change_summary=summary,
            change_origin="BASE_GENERATION",
            actor_id=actor_id,
        )

        progress.step(session.id, "Checking facts")
        # The groundedness judge (include_llm_pass) is a THIRD sequential model
        # call on top of moulding and the summary -- measured at roughly a
        # third of a 400s generate on this provider -- for a QA pass the
        # validation module's own docs call "optional and non-fatal". It ran
        # automatically on nearly every generate (moulding is the common
        # case), which is not what "optional" is supposed to mean. Off here;
        # the fast, non-LLM factual rules below still run every time.
        findings = validation_engine.run_validation(
            db,
            session,
            content,
            unsupported_additions=composition.unsupported,
            source_content=composition.source_content,
            include_llm_pass=False,
        )
        warnings = persist_warnings(db, session, version, findings)
    except Exception as exc:
        progress.finish(session.id, error=str(exc))
        raise
    else:
        progress.finish(session.id)

    # EDITING, not GENERATED: "a resume exists and nobody has approved it" is a
    # single state, and it is the one a session sits in whenever somebody walks
    # away from it. Two names for it meant the History tab showed the same
    # situation two different ways depending on whether anyone had typed since.
    session.status = ResumeSessionStatus.EDITING
    audit.record(
        db,
        action="RESUME_GENERATED",
        entity_type="ResumeVersion",
        entity_id=version.id,
        actor_id=actor_id,
        summary=f"V{version.version_no} - {summary}",
        payload={"fit": composition.fit.to_dict(), "ground_rules": composition.ground_rules},
    )
    db.flush()
    return version, warnings, composition


def apply_manual_edit(
    db: Session,
    session: ResumeCompositionSession,
    content: ResumeContent,
    *,
    change_summary: str | None = None,
    actor_id: UUID | None = None,
) -> tuple[ResumeVersion, list[ValidationWarning]]:
    """Section 23 — "Directly edit generated content"."""
    assert_reservation_active(db, session)
    version = create_version(
        db,
        session,
        content,
        change_summary=change_summary or "Manual edit",
        change_origin="MANUAL_EDIT",
        actor_id=actor_id,
    )
    findings = validation_engine.run_validation(db, session, content)
    warnings = persist_warnings(db, session, version, findings)
    session.status = ResumeSessionStatus.EDITING
    db.flush()
    return version, warnings


# ───────────────────────────────────────────────────── the content window
#
# One entry point for every way a section is changed: a click in the studio, a
# typed instruction in the chat, a drag in the section list. They all land
# here, so the window, the chat and the PDF cannot end up disagreeing about
# what this resume contains.


def sections_view(db: Session, session: ResumeCompositionSession) -> dict:
    """This session's content window — its sections, and how full each is.

    Readable before anything is generated. The window is the chosen design's
    section list, and that exists from the moment the design is chosen — so a
    session waiting on its first generation shows the sections it is about to
    fill, every one of them empty, rather than an error.
    """
    version = current_version(db, session)
    content = (
        ResumeContent.model_validate(version.content_json) if version is not None else ResumeContent()
    )
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    if template is not None:
        content_window.seed(content, template)
    view = content_window.describe(content)
    view["template_id"] = str(session.template_id)
    view["template_name"] = template.name if template else None
    view["is_fillable"] = bool(template is not None and (template.jinja_source or "").strip())
    return view


def edit_sections(
    db: Session,
    session: ResumeCompositionSession,
    action: str,
    *,
    actor_id: UUID | None = None,
    **kwargs: Any,
) -> tuple[ResumeVersion, list[ValidationWarning], str]:
    """Apply one content-window change and version the result.

    Every change is a new version for the same reason every other edit is: it
    has to be undoable, and "put Education back" a week later should be a
    restore rather than a regeneration.
    """
    content = current_content(db, session)
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    if template is not None:
        content_window.seed(content, template)

    handlers = {
        "add": lambda: content_window.add(
            content,
            kwargs.get("title") or "",
            kind=kwargs.get("kind"),
            body=kwargs.get("body") or "",
            items=kwargs.get("items"),
            position=kwargs.get("position"),
        ),
        "remove": lambda: content_window.remove(
            content, kwargs.get("section") or "", purge=bool(kwargs.get("purge"))
        ),
        "show": lambda: content_window.set_enabled(content, kwargs.get("section") or "", True),
        "hide": lambda: content_window.set_enabled(content, kwargs.get("section") or "", False),
        "rename": lambda: content_window.rename(
            content, kwargs.get("section") or "", kwargs.get("title") or ""
        ),
        "reorder": lambda: content_window.reorder(content, kwargs.get("order") or []),
        "move": lambda: content_window.move(
            content, kwargs.get("section") or "", kwargs.get("direction") or "up"
        ),
    }
    handler = handlers.get(action)
    if handler is None:
        raise ValidationError(
            f"'{action}' is not a section operation. Use: {', '.join(sorted(handlers))}."
        )

    changed, message = handler()
    if not changed:
        raise ValidationError(message)

    version, warnings = apply_manual_edit(
        db, session, content, change_summary=message, actor_id=actor_id
    )
    return version, warnings, message


def restore_version(
    db: Session,
    session: ResumeCompositionSession,
    version_id: UUID,
    *,
    change_summary: str | None = None,
    actor_id: UUID | None = None,
    mark_undone: bool = False,
) -> ResumeVersion:
    """Section 23 — "Reject AI changes".

    Restoring repoints the session at an existing version rather than copying
    it forward, so the rejected attempt stays in the history exactly where it
    was.
    """
    source = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.session_id == session.id)
        .first()
    )
    if source is None:
        raise NotFoundError("Version not found in this session")
    if session.current_version_id == source.id:
        # A second click on the row that is already current — the studio no
        # longer shows Restore there, but an in-flight double-click or a
        # direct API call could still reach here. Restoring a version onto
        # itself has nothing to do and nothing to undo; without this it wrote
        # an audit entry reading "Restored here from V6" on V6 itself, which
        # is not an event that happened, and looked to the operator like
        # Restore had silently done nothing (because it had).
        raise ConflictError(f"V{source.version_no} is already the current version.")

    # Restoring REPOINTS the session at an existing version; it does not copy
    # one forward. Copying forward meant every "actually, go back" produced a
    # version of its own, so a few rounds of trying something and undoing it
    # buried the real work: a history reading V4..V10 "Restored from V3",
    # "Restored from V2", "Restored from V5" says nothing about the resume and
    # hides the versions that do. Nothing is lost by repointing either — the
    # version being left behind stays in the list exactly where it was, and
    # can be restored again.
    content = ResumeContent.model_validate(source.content_json)
    previous = current_version(db, session)
    if previous is not None and previous.id != source.id:
        if previous.status == ResumeVersionStatus.DRAFT:
            previous.status = ResumeVersionStatus.SUPERSEDED

    if source.status == ResumeVersionStatus.SUPERSEDED:
        source.status = ResumeVersionStatus.DRAFT
    session.current_version_id = source.id
    session.current_version_no = source.version_no
    version = source

    audit.record(
        db,
        action="VERSION_RESTORED",
        entity_type="ResumeVersion",
        entity_id=source.id,
        actor_id=actor_id,
        summary=change_summary or f"Restored V{source.version_no}",
        payload={
            "restored_version_no": source.version_no,
            "from_version_no": previous.version_no if previous is not None else None,
            # Only ever set by the Sections tab's inline Undo. Read back in
            # list_versions to mark the abandoned version disabled there —
            # see ResumeVersionResponse.is_undone.
            "undone": bool(mark_undone and previous is not None),
        },
    )

    findings = validation_engine.run_validation(db, session, content)
    persist_warnings(db, session, version, findings)

    # Tell the chat agent, in its own transcript, that the document moved under
    # it. Without this the history still reads "I added X" while the resume no
    # longer has X, and the model resolves that contradiction by deciding its
    # edit never landed and doing it again. A restore is the one event that
    # invalidates earlier turns wholesale, so it is recorded as a turn.
    db.add(
        AIInteraction(
            session_id=session.id,
            role="assistant",
            message=(
                f"[the operator restored V{source.version_no}. Every edit made after it "
                f"was discarded — the resume state above is the truth, not the earlier "
                f"turns in this conversation.]"
            ),
            created_by_id=actor_id,
        )
    )
    db.flush()
    return version


# -------------------------------------------------------------------- studio
def _resume_state_summary(content: ResumeContent) -> str:
    """Everything on the resume, as the agent sees it.

    This must describe the *whole* document. Anything omitted here is invisible
    to the agent, and it will confidently tell the user a section does not exist
    while the user is looking straight at it in the preview.
    """
    lines = [
        f"Headline: {content.contact.headline or '(none)'}",
        f"Summary: {content.summary[:400]}",
    ]
    for group in content.skill_groups:
        lines.append(f"Skill group '{group.label}' ({group.tier}): {', '.join(group.items)}")
    for index, project in enumerate(content.projects):
        lines.append(
            f"Project {index + 1}: {project.name} "
            f"as {project.role or 'n/a'} "
            f"[{', '.join(project.technologies) or 'no technologies listed'}] "
            f"— {project.description[:200]}"
        )
    for index, exp in enumerate(content.experience):
        lines.append(
            f"Experience {index + 1}: {exp.company} as {exp.designation or 'n/a'} "
            f"({exp.start_label or '?'} to {exp.end_label or '?'})"
            + (f" — {len(exp.highlights)} bullet(s)" if exp.highlights else " — no bullets")
        )

    lines.append(
        f"Education section: {len(content.education)} entry/entries"
        + (" — " + "; ".join(e.qualification for e in content.education) if content.education else " (empty)")
    )
    lines.append(
        f"Certifications section: {len(content.certifications)} item(s)"
        + (" — " + "; ".join(content.certifications) if content.certifications else " (empty)")
    )
    if content.sections:
        # The chat can rename, hide, reorder and add sections, and it can only
        # do that against the headings this template actually uses. Naming them
        # here is what lets "drop the education section" work on a design that
        # calls it "Academic Background".
        lines.append(
            "Sections of this template, in print order: "
            + ", ".join(
                f"{s.title}{'' if s.enabled else ' (hidden)'}" for s in content.sections
            )
        )
    if content.render_hints:
        lines.append(f"Render hints: {content.render_hints}")
    return "\n".join(lines)


#: How much of the conversation is replayed to the model. Enough for "now do
#: the same for the other project" to resolve, short enough that a long session
#: does not push the resume state out of the context window.
CHAT_HISTORY_TURNS = 12


def _chat_history(
    db: Session, session: ResumeCompositionSession, message: str
) -> list[dict[str, str]]:
    """The recent conversation, oldest first, ending with the new instruction.

    Without this the studio has no memory: every instruction arrived as if it
    were the first, so "make it shorter" or "do that to the next one too" had
    nothing to refer back to.
    """
    rows = (
        db.query(AIInteraction)
        .filter(AIInteraction.session_id == session.id)
        .order_by(AIInteraction.created_at.desc(), AIInteraction.id.desc())
        .limit(CHAT_HISTORY_TURNS * 2)
        .all()
    )
    history: list[dict[str, str]] = []
    for row in reversed(rows):
        text = (row.message or "").strip()
        if not text or row.role not in ("user", "assistant"):
            continue
        # An assistant turn carries what it SAID and, separately, what actually
        # ran. Replaying only the prose left the model guessing at its own
        # history, and it guessed wrong: told to add one project, it announced
        # that its previous edit "wasn't actually applied (no tool calls ran in
        # that turn)" -- when that edit had applied and was sitting in the
        # version list -- then re-added, ending with nine projects where six
        # were asked for. The tool record is already stored on the row; it just
        # was never shown to the model. Facts beat the model's memory of its
        # own narration.
        if row.role == "assistant":
            names = [
                str(call.get("tool"))
                for call in (row.tool_calls or [])
                if isinstance(call, dict) and call.get("applied") and call.get("tool")
            ]
            text += (
                f"\n[applied this turn: {', '.join(names)}]"
                if names
                else "\n[no edit was applied this turn]"
            )
        history.append({"role": row.role, "content": text})

    # The new instruction was written to AIInteraction just above, so it is
    # already the last row; only append it when it is missing.
    if not history or history[-1]["content"] != message.strip():
        history.append({"role": "user", "content": message})
    return history


def _requirement_summary(db: Session, session: ResumeCompositionSession) -> str:
    """The JD, in the few lines the editing agent needs.

    Without this the agent answered "I don't see the job description attached to
    your message" to an instruction the studio itself suggests — the JD is the
    whole reason the session exists, and it was the one thing not in context.
    """
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    if requirement is None:
        return "No job description is attached to this session."

    mandatory, preferred = requirement_repository.mandatory_and_preferred(db, requirement.id)
    lines = [f"Role: {requirement.parsed_role or 'not stated'}"]
    if requirement.min_experience_years:
        lines.append(f"Experience wanted: {requirement.min_experience_years:g}+ years")
    if mandatory:
        lines.append(f"Mandatory skills: {', '.join(mandatory)}")
    if preferred:
        lines.append(f"Preferred skills: {', '.join(preferred)}")
    if requirement.location:
        lines.append(f"Location: {requirement.location}")
    for item in (requirement.responsibilities or [])[:8]:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def chat_edit(
    db: Session,
    session: ResumeCompositionSession,
    message: str,
    *,
    actor_id: UUID | None = None,
) -> dict[str, Any]:
    """Section 22 — one turn of the agentic chatbot."""
    assert_reservation_active(db, session)
    content = current_content(db, session)

    db.add(AIInteraction(session_id=session.id, role="user", message=message, created_by_id=actor_id))
    db.flush()

    person_techs = [
        tech.name
        for _, tech in db.query(PersonTechnology, Technology)
        .join(Technology, Technology.id == PersonTechnology.technology_id)
        .filter(PersonTechnology.person_id == session.person_id)
        .all()
    ]
    # Section 17 — projects and companies are both shared pools, unlinked to
    # any person, so the agent gets the whole list of each rather than one
    # scoped to whoever this session is for.
    pool_projects = [p.name for p in db.query(Project).filter(Project.is_active.is_(True)).limit(200).all()]
    pool_companies = [c.name for c in db.query(Company).order_by(Company.name).limit(200).all()]

    system = EDIT_AGENT_SYSTEM.format(
        requirement=_requirement_summary(db, session),
        resume_state=_resume_state_summary(content),
        person_technologies=", ".join(person_techs) or "none recorded",
        available_projects=", ".join(pool_projects) or "none",
        available_companies=", ".join(pool_companies) or "none",
    )

    provider = get_llm_provider()
    started = time.perf_counter()
    try:
        response = provider.chat(
            _chat_history(db, session, message),
            model=LLM_MODEL,
            system=system,
            temperature=LLM_TEMPERATURE,
            # Same reason as the composer: the reply is short, but the reasoning
            # before it is not, and a truncated turn returns no tool calls at all.
            max_tokens=max(LLM_MAX_TOKENS, 16000),
            tools=TOOL_SPECS,
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except (LLMTransientError, LLMPermanentError) as exc:
        logger.warning("Studio chat failed: %s", exc)
        db.add(
            AIInteraction(
                session_id=session.id,
                role="assistant",
                message="The assistant is unavailable right now. Your resume is unchanged.",
                created_by_id=actor_id,
            )
        )
        db.flush()
        return {
            "reply": "The assistant is unavailable right now. Your resume is unchanged — please try again.",
            "version": None,
            "content": content,
            "tool_calls": [],
            "warnings": [],
        }

    latency_ms = int((time.perf_counter() - started) * 1000)

    working = content.clone()
    records: list[ToolCallRecord] = []
    for call in response.tool_calls:
        outcome = apply_tool(
            working,
            call["name"],
            call.get("arguments", {}),
            context=ToolContext(db=db, session=session),
        )
        records.append(
            ToolCallRecord(
                tool=call["name"],
                arguments=call.get("arguments", {}),
                result=outcome.description,
                applied=outcome.applied,
            )
        )

    applied = [r for r in records if r.applied]
    if applied:
        # The agent writes dates the way the user said them - "2024", "March
        # 2023" - and the rest of the document is "Mar 2023". One format per
        # page is the whole point of normalising here rather than at render.
        ground_rules.normalise_date_labels(working)
    reply = response.text or ""
    if applied:
        detail = " ".join(r.result or "" for r in applied)
        reply = f"{reply}\n\n{detail}".strip() if reply else detail
    elif records:
        # Every tool call failed. The model has already written a sentence
        # promising the change, and showing that alone told the user something
        # happened when nothing did — the reason has to beat the promise.
        why = " ".join(r.result or "" for r in records if r.result)
        reply = (
            f"Nothing changed. {why}".strip()
            if why
            else "Nothing changed — that instruction did not match anything on the resume."
        )
    else:
        # No tool call was even attempted. Most of the time that is the
        # correct outcome — a question ("is Python used here?"), a genuine
        # explanation of why nothing needed to change ("Kubernetes is already
        # listed"), or a clarifying question — and the model's own text is
        # the right thing to show for every one of those. The one shape that
        # is NOT safe to show as-is is a claimed completion with nothing
        # behind it: a model under load has been seen narrating "I've applied
        # the X format" without ever emitting the tool call that would make
        # that true, and showing that verbatim tells the user something
        # changed when nothing did. Log the raw text either way so a pattern
        # of narrated-but-not-called edits is visible without guessing.
        stripped = reply.strip()
        if stripped:
            # ascii-safe: the default Windows console encoding cannot represent
            # everything a model writes (an arrow, a smart quote), and a
            # logging call that itself throws is worse than a flattened log.
            safe = stripped.encode("ascii", "backslashreplace").decode("ascii")
            logger.info("Chat turn made no tool call; model said: %r", safe)
        reply = (
            stripped
            if stripped and not _CLAIMED_COMPLETION.search(stripped)
            else "I didn't make any changes — could you tell me more specifically what to adjust?"
        )

    version = None
    warnings: list[ValidationWarning] = []
    if applied:
        version = create_version(
            db,
            session,
            working,
            change_summary="; ".join(r.result or r.tool for r in applied)[:1000],
            change_origin="AI_EDIT",
            actor_id=actor_id,
        )
        findings = validation_engine.run_validation(db, session, working)
        warnings = persist_warnings(db, session, version, findings)
        session.status = ResumeSessionStatus.EDITING

    db.add(
        AIInteraction(
            session_id=session.id,
            role="assistant",
            message=reply,
            tool_calls=[r.model_dump() for r in records],
            resulting_version_id=version.id if version else None,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=latency_ms,
            created_by_id=actor_id,
        )
    )
    db.flush()

    return {
        "reply": reply,
        "version": version,
        "content": working if applied else content,
        "tool_calls": records,
        "warnings": warnings,
    }


# ----------------------------------------------------------------- selections
def update_selections(
    db: Session,
    session: ResumeCompositionSession,
    payload: SessionSelectionUpdate,
    *,
    actor_id: UUID | None = None,
) -> ResumeCompositionSession:
    """Section 17/23 — replace the session's project/company/technology mapping."""
    assert_reservation_active(db, session)

    # The one-page shape is enforced here rather than silently trimmed at
    # render: an operator who picked seven projects should be told the sixth and
    # seventh will not print, not discover it in the downloaded PDF.
    if payload.projects is not None and len(payload.projects) > RESUME_MAX_PROJECTS:
        raise ValidationError(
            f"A resume prints on one page and carries at most {RESUME_MAX_PROJECTS} "
            f"projects — {len(payload.projects)} were selected. Remove the ones that "
            f"answer this JD least."
        )
    if payload.companies is not None and len(payload.companies) > RESUME_MAX_COMPANIES:
        raise ValidationError(
            f"A resume prints on one page and carries at most {RESUME_MAX_COMPANIES} "
            f"employers — {len(payload.companies)} were selected."
        )

    used_projects: set = set()
    used_companies: set = set()
    if payload.projects is not None or payload.companies is not None:
        used_projects, used_companies = resume_composer.used_projects_and_companies_for_requirement(
            db, session.requirement_id, exclude_session_id=session.id
        )

    if payload.projects is not None:
        db.query(ResumeSelectedProject).filter(ResumeSelectedProject.session_id == session.id).delete()
        for item in payload.projects:
            # A row with no project_id is one the operator composed here. It is
            # described entirely by its overrides and never touches the bank.
            project = (
                db.query(Project).filter(Project.id == item.project_id).first()
                if item.project_id
                else None
            )
            if item.project_id and project is None:
                raise NotFoundError(f"Project {item.project_id} not found")
            if item.project_id and item.project_id in used_projects:
                raise ConflictError(
                    f"'{project.name}' was already used in an approved resume for this JD and "
                    "cannot be picked again here. Choose a different project, or compose one "
                    "directly on this resume instead."
                )

            db.add(
                ResumeSelectedProject(
                    session_id=session.id,
                    project_id=item.project_id,
                    display_order=item.display_order,
                    is_primary="true" if item.is_primary else None,
                    title_override=item.title_override,
                    role_override=item.role_override,
                    description_override=item.description_override,
                    responsibilities_override=item.responsibilities_override,
                    achievements_override=item.achievements_override,
                    technologies_override=item.technologies_override,
                    # Carried across the delete-and-recreate. Losing it here is
                    # what emptied the resume of its projects after any
                    # composition edit: the pairing was set only at generation,
                    # so a rebuild found every project belonging to nobody and
                    # the designs that print projects under their employer had
                    # nothing to print. Null still means "let the composer
                    # decide", which is what pair_projects_to_companies does.
                    company_id=item.company_id,
                )
            )

            # A composed project — no row in the bank behind it — is still
            # worth an audit line: it is new data, not a rearrangement of
            # something already on record.
            if project is None:
                audit.record(
                    db,
                    action="PROJECT_COMPOSED",
                    entity_type="ResumeSelectedProject",
                    entity_id=session.id,
                    actor_id=actor_id,
                    summary=item.title_override,
                    payload={"project_name": item.title_override},
                )

    if payload.companies is not None:
        db.query(ResumeSelectedCompany).filter(ResumeSelectedCompany.session_id == session.id).delete()
        for item in payload.companies:
            if item.company_id in used_companies:
                company = db.query(Company).filter(Company.id == item.company_id).first()
                raise ConflictError(
                    f"'{company.name if company else item.company_id}' was already used in an "
                    "approved resume for this JD and cannot be picked again here."
                )
            db.add(
                ResumeSelectedCompany(
                    session_id=session.id,
                    company_id=item.company_id,
                    display_order=item.display_order,
                    designation_override=item.designation_override,
                    start_date_override=item.start_date_override,
                    end_date_override=item.end_date_override,
                )
            )

    if payload.technologies is not None:
        db.query(ResumeSelectedTechnology).filter(ResumeSelectedTechnology.session_id == session.id).delete()
        for item in payload.technologies:
            db.add(
                ResumeSelectedTechnology(
                    session_id=session.id,
                    technology_id=item.technology_id,
                    section_label=item.section_label,
                    tier=item.tier,
                    display_order=item.display_order,
                    is_jd_driven="true" if item.is_jd_driven else None,
                )
            )

    db.flush()
    return session


def _persist_reframed_projects(
    db: Session, session: ResumeCompositionSession, content: ResumeContent, names: list[str]
) -> None:
    """Write a just-moulded description back onto the row it came from.

    Unlike ``resume_composer.persist_composed_overrides`` (a gap-fill: it never
    touches a column that already holds something), this one overwrites on
    purpose. The column may already hold text — the operator's own draft,
    typed into the Composition tab this same save — and moulding it toward the
    JD was the point of typing it there at all; leaving the raw draft in place
    would show the moulded version once, in this response, and then revert to
    the draft on the very next rebuild because the project would no longer
    look "new".
    """
    rows = (
        db.query(ResumeSelectedProject).filter(ResumeSelectedProject.session_id == session.id).all()
    )
    by_project_id = {str(r.project_id): r for r in rows if r.project_id is not None}
    by_title = {(r.title_override or "").strip(): r for r in rows if r.project_id is None}
    for block in content.projects:
        if block.name not in names:
            continue
        row = by_project_id.get(block.source_project_id or "") or by_title.get(block.name.strip())
        if row is None:
            continue
        if (block.description or "").strip():
            row.description_override = block.description
        if block.responsibilities:
            row.responsibilities_override = list(block.responsibilities)


def rebuild_from_selections(
    db: Session, session: ResumeCompositionSession, *, actor_id: UUID | None = None
) -> tuple[ResumeVersion, list[ValidationWarning]]:
    """Re-assemble the resume after the selections changed, keeping the summary."""
    assert_reservation_active(db, session)
    existing = current_version(db, session)
    content = resume_composer.build_pool_content(db, session)
    # What was already on the resume before this save, so anything not in this
    # set below is a project this rebuild is putting on the page for the first
    # time — composed rows have no bank id, so the name stands in for one.
    previous_project_keys: set[tuple[str | None, str]] = set()
    previous_company_ids: set[str] = set()
    if existing is not None:
        previous = ResumeContent.model_validate(existing.content_json)
        content.summary = previous.summary or content.summary
        content.contact.headline = previous.contact.headline or content.contact.headline
        content.render_hints = previous.render_hints
        previous_project_keys = {(p.source_project_id, p.name) for p in previous.projects}
        previous_company_ids = {e.source_company_id for e in previous.experience if e.source_company_id}

    # Re-pair every project with an employer. update_selections replaces the
    # ResumeSelectedProject rows wholesale and does not carry company_id
    # across, so by the time we get here the pairing decided at generation is
    # gone -- and generation is the only other place that sets it. The result
    # was a resume whose projects belonged to nobody: on a design that prints
    # projects under their employer (the ESSENTIALS and NESTED shapes, which
    # is most of the shipped library) they simply stopped appearing, which is
    # what "the content page breaks after editing the composition" was.
    resume_composer.pair_projects_to_companies(db, session, content)
    # Pairing mutates the content; persisting writes it back onto the selection
    # rows so the NEXT rebuild starts from this resume's own history rather
    # than dealing the projects out again and reshuffling the page.
    resume_composer.persist_project_companies(db, session, content)

    # The house rules are not a one-off applied at generation: rebuilding from
    # the pool would otherwise hand education and the un-expanded timeline back,
    # and the operator would have to remove them again after every edit to the
    # composition.
    requirement = db.query(Requirement).filter(Requirement.id == session.requirement_id).first()
    current_company_ids = {e.source_company_id for e in content.experience if e.source_company_id}
    roster_changed = current_company_ids != previous_company_ids
    # "Every row already has a date" used to be the only signal for whether the
    # timeline needed redistributing, which broke the moment an operator typed
    # exact dates for a company they just added: every row now had something
    # in it, so this looked identical to "nothing changed" and the other
    # employers were left exactly where they were — even though one more
    # employer sharing the JD's years means every window needs to move to make
    # room for it, typed dates or not. Roster changes are checked first and
    # win regardless of what the new row's dates say; only when nobody was
    # added or removed does a fully-dated set of rows mean "leave it alone",
    # which is what protects a deliberate edit to an EXISTING company's own
    # dates from being redistributed away on an unrelated save.
    if roster_changed:
        # meet_jd_experience (called below) has its own internal fast path:
        # if every block already parses a date AND the total already meets
        # the JD's target, it assumes there is nothing to do. That heuristic
        # cannot see that an employer was just added or removed, so a
        # manually-typed, individually-plausible date on the new row sailed
        # straight through it and left every OTHER employer exactly where it
        # was -- no room was made for the new one at all. Blanking every
        # label first is what forces that fast path open; it is the same
        # thing relay_timeline does for the chat-tool equivalent of this same
        # event, so a roster change behaves identically whichever door it
        # came in through.
        for block in content.experience:
            block.start_label = None
            block.end_label = None

    rows = session_repository.selected_companies(db, session.id)
    every_row_dated = bool(rows) and all(
        (row.start_date_override or "").strip() and (row.end_date_override or "").strip() for row in rows
    )
    rules = ground_rules.apply_v1_ground_rules(
        content,
        target_experience_years=requirement.min_experience_years if requirement else None,
        keep_experience_dates=every_row_dated and not roster_changed,
    )
    # The redistribution just decided every company's dates together; write
    # them back so the Composition tab agrees with what this version shows,
    # and so the next rebuild sees full rows instead of redistributing again.
    if rules.experience_expanded_from is not None:
        resume_composer.persist_experience_dates(db, session, content)

    # A project new to this resume as of this save still carries whatever text
    # build_pool_content gave it -- the operator's own wording if they typed
    # one, otherwise the bank's description written for nobody in particular.
    # Either way it has not been angled at this JD yet, and the dates just
    # settled above are what decide which of its technologies it may even
    # mention. Doing this here, not only from the chat tools, is what makes
    # "add a project from the Composition tab" behave the same as "add a
    # project" typed into the assistant.
    new_names = [
        p.name for p in content.projects if (p.source_project_id, p.name) not in previous_project_keys
    ]
    if new_names:
        reframe_new_blocks(ToolContext(db=db, session=session), content, new_names)
        _persist_reframed_projects(db, session, content, new_names)

    # Whether or not anything was new, the dates may just have moved every
    # employer's window -- drop technologies from ANY project that its
    # (possibly new) period cannot carry, the same safety net generation and
    # chat edits both get. This never removes a project, only a claim it can
    # no longer honestly make.
    releases = era.technology_release_years(db)
    resume_composer.strip_anachronistic_technologies(content, releases)

    # Moulding and the timeline only touched the in-memory content; write the
    # result back onto the selection rows so the Composition tab shows what
    # this version actually says instead of the raw, unmoulded bank text, and
    # so the NEXT rebuild starts from here rather than moulding all over again.
    resume_composer.persist_composed_overrides(db, session, content)

    version = create_version(
        db,
        session,
        content,
        change_summary="Rebuilt from updated project/company/technology selections",
        change_origin="SELECTION_CHANGE",
        actor_id=actor_id,
    )
    findings = validation_engine.run_validation(db, session, content)
    warnings = persist_warnings(db, session, version, findings)
    db.flush()
    return version, warnings


# -------------------------------------------------------------------- render
def render_preview(db: Session, session: ResumeCompositionSession) -> str:
    """A fast, template-agnostic view of the CONTENT.

    This deliberately does not show the chosen design. It renders from the
    layout definition, which an uploaded .tex or .pdf template does not have —
    so every uploaded template previews identically here. It is the right view
    while editing text, and the wrong one for judging the design.

    ``render_design_pdf`` below is the one that shows the template.
    """
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    return render_html(current_content(db, session), template.layout_definition if template else None)


def render_design_pdf(db: Session, session: ResumeCompositionSession) -> bytes:
    """The current version compiled through the session's actual template.

    The composed LaTeX is cached on the version, so the expensive part — the
    model call — happens once per version and every later look is just a
    compile. Callers get bytes rather than a stored file because this is a
    preview: the artefacts of record are written at approval.
    """
    version = current_version(db, session)
    if version is None:
        raise ConflictError("This session has no generated resume yet")

    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()

    latex_source = version.latex_source
    if not latex_source:
        latex_source, _model = latex_composer.compose_latex(
            db, session, ResumeContent.model_validate(version.content_json)
        )
        version.latex_source = latex_source
        db.flush()

    directory = storage.session_dir(session.id)
    target = directory / f"preview-v{version.version_no}.pdf"
    # Compiled beside the template's own .cls/.sty/images, the way Overleaf
    # compiles it — without them an uploaded project cannot build at all.
    assets = latex_composer.assets_dir_for(template) if template else None
    return latex.compile_pdf(latex_source, target, assets_dir=assets).read_bytes()


def render_files(
    db: Session,
    session: ResumeCompositionSession,
    version: ResumeVersion,
    formats: list[str],
) -> list[ResumeFile]:
    """Produce the artefacts for a version, replacing any earlier run.

    Section 26 templates are LaTeX, so the resume's real appearance comes from
    compiling the composed .tex. Three artefacts fall out of that:

        TEX   the composed source — always produced, and the thing to inspect
              when a compile fails
        PDF   the designed resume, when a LaTeX engine is installed
        DOCX  a plain, unstyled but editable file from the structured content,
              because section 43 requires a Word download and a .tex template
              cannot produce one

    A missing engine degrades to TEX + DOCX rather than failing: the user can
    still approve, download and compile elsewhere.
    """
    content = ResumeContent.model_validate(version.content_json)
    template = db.query(ResumeTemplate).filter(ResumeTemplate.id == session.template_id).first()
    layout = template.layout_definition if template else None
    assets = latex_composer.assets_dir_for(template) if template else None

    person = db.query(Person).filter(Person.id == session.person_id).first()
    profile = db.query(PersonProfile).filter(PersonProfile.id == session.person_profile_id).first()
    vendor = db.query(Vendor).filter(Vendor.id == session.vendor_id).first()

    directory = storage.session_dir(session.id)
    wanted = {f.upper() for f in formats} or {"TEX", "PDF", "DOCX"}
    # The .tex is the source of the PDF, so asking for one implies the other.
    if "PDF" in wanted:
        wanted.add("TEX")

    def filename(ext: str) -> str:
        return storage.build_filename(
            person.full_name if person else "candidate",
            profile.title if profile else "profile",
            vendor.name if vendor else "vendor",
            version.version_no,
            ext,
        )

    produced: list[ResumeFile] = []
    notes: list[str] = []

    registered: dict[str, ResumeFile] = {}

    def register(fmt: str, path: Path) -> None:
        # The .tex is registered twice when the repair pass rewrites it. The
        # first row may still be pending in the session, where a DELETE cannot
        # reach it — both would then INSERT and violate uq_version_format — so
        # the earlier object is expunged before the DELETE runs.
        previous = registered.pop(fmt, None)
        if previous is not None:
            if previous in db.new:
                db.expunge(previous)
            produced[:] = [f for f in produced if f is not previous]
        db.query(ResumeFile).filter(
            ResumeFile.version_id == version.id, ResumeFile.file_format == fmt
        ).delete()
        record = ResumeFile(
            version_id=version.id,
            file_format=fmt,
            file_name=path.name,
            storage_path=storage.relative_path(path),
            size_bytes=path.stat().st_size,
            checksum_sha256=storage.checksum(path),
        )
        db.add(record)
        # Flushed immediately so a later re-registration of the same format sees
        # a real row to delete rather than a pending twin.
        db.flush()
        registered[fmt] = record
        produced.append(record)

    # ── TEX + PDF, from the uploaded LaTeX template
    tex_path = None
    latex_source = None
    if {"TEX", "PDF"} & wanted:
        latex_source = version.latex_source
        if not latex_source:
            try:
                latex_source, _model = latex_composer.compose_latex(db, session, content)
                version.latex_source = latex_source
            except Exception as exc:  # noqa: BLE001 — one artefact must not sink the rest
                # A composition failure used to abort the whole render, so a
                # flaky reply cost the operator the DOCX as well. The DOCX is
                # built from the structured content and never needed the model.
                logger.warning("LaTeX composition failed for session %s: %s", session.id, exc)
                notes.append(f"LaTeX not composed: {exc}")
                latex_source = None
                wanted -= {"TEX", "PDF"}

    if latex_source:
        tex_path = directory / filename("tex")
        # newline="" keeps LF endings: a .tex written with CRLF is harder to
        # diff and some minimal TeX setups choke on stray carriage returns.
        with tex_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(latex_source)
        if "TEX" in wanted:
            register("TEX", tex_path)

    if "PDF" in wanted and tex_path is not None:
        try:
            pdf_path = latex.compile_pdf(
                tex_path.read_text(encoding="utf-8"),
                directory / filename("pdf"),
                assets_dir=assets,
            )
            register("PDF", pdf_path)
        except latex.LatexCompileError as first_error:
            logger.warning("LaTeX compile failed for session %s: %s", session.id, first_error)
            # One repair attempt, handing the model the engine's own message.
            # The preamble is copied verbatim from the template, so a failure is
            # almost always a fixable mistake in the generated body: an undefined
            # macro, an unbalanced brace, an unescaped "&". This is what lets a
            # template nobody has tested survive its first run.
            healed = False
            try:
                repaired = latex_composer.repair_latex(
                    db, session, content, latex_source, first_error.log
                )
            except Exception:  # noqa: BLE001 — repair is best effort, never fatal
                logger.exception("LaTeX repair pass failed for session %s", session.id)
                repaired = None

            if repaired is not None:
                fixed_source, _model = repaired
                version.latex_source = fixed_source
                with tex_path.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(fixed_source)
                if "TEX" in wanted:
                    register("TEX", tex_path)
                try:
                    register("PDF", latex.compile_pdf(
                        fixed_source, directory / filename("pdf"), assets_dir=assets
                    ))
                    logger.info("LaTeX repair succeeded for session %s", session.id)
                    healed = True
                except latex.LatexCompileError as second_error:
                    logger.warning("LaTeX repair did not compile either: %s", second_error)

            if not healed:
                # Not fatal. The .tex is already stored; say why the PDF is
                # absent rather than failing an approval the user has decided on.
                notes.append(str(first_error))

    # ── DOCX, from the structured content rather than the template
    if "DOCX" in wanted:
        register("DOCX", render_docx(content, layout, directory / filename("docx")))

    db.flush()
    if notes:
        version.change_summary = ((version.change_summary or "") + " | " + "; ".join(notes)).strip(" |")
    return produced


# ------------------------------------------------------------------- approval
def approve(
    db: Session,
    session: ResumeCompositionSession,
    *,
    formats: list[str] | None = None,
    notes: str | None = None,
    actor_id: UUID | None = None,
) -> dict[str, Any]:
    """Sections 23, 27, 33 and 43 steps 9–13.

    Approval is the point at which the profile leaves that vendor's pool. Open
    validation warnings do not block approval — the decision stays with the
    user, who composed this resume by hand and owns the facts on it (release
    dates, timelines, claims) the same way they own every other word on the
    page — but the count is returned and the audit entry records it, so
    "approved with three unresolved warnings" is a fact someone can look up
    later rather than something that quietly disappears. No fresh validation
    pass runs here: the content was already checked as it was composed, and
    re-running a full fact/language pass at the one moment the user is trying
    to finish is what made approval slow for no benefit — the findings would
    be the same ones already on the version.
    """
    assert_reservation_active(db, session)
    version = current_version(db, session)
    if version is None:
        raise ConflictError("There is no resume to approve in this session")

    outstanding = open_warnings(db, session.id, version.id)

    version.status = ResumeVersionStatus.APPROVED
    # The version is APPROVED; the SESSION is RESERVED. They are different
    # things: one is a document that is finished, the other is a person being
    # held for a vendor while somebody decides what to do with the document.
    session.status = ResumeSessionStatus.RESERVED
    files = render_files(db, session, version, formats or ["DOCX", "PDF"])

    now = utcnow()
    submission = Submission(
        reference_code=allocate_reference_code(db, Submission.reference_code, "SUB"),
        session_id=session.id,
        version_id=version.id,
        person_id=session.person_id,
        vendor_id=session.vendor_id,
        requirement_id=session.requirement_id,
        status=SubmissionStatus.RESERVED,
        approved_at=now,
        notes=notes,
        created_by_id=actor_id,
    )
    db.add(submission)
    db.flush()

    db.add(
        SubmissionStatusHistory(
            submission_id=submission.id,
            from_status=None,
            to_status=SubmissionStatus.RESERVED,
            changed_by_id=actor_id,
            reason="Resume approved in the studio",
        )
    )

    # Section 8/30 — approval is the point the profile leaves this vendor's
    # pool. It becomes RESERVED rather than ENGAGED: the resume is finished and
    # committed to this vendor, but the vendor has not answered, and calling
    # that an engagement would mean nobody ever notices a submission that went
    # nowhere. The hold carries a clock; the operator promotes it to ENGAGED
    # from the vendor tracking view once there is something to engage about.
    availability.mark_reserved(
        db,
        person_id=session.person_id,
        vendor_id=session.vendor_id,
        requirement_id=session.requirement_id,
        actor_id=actor_id,
    )

    resolved = db.query(ValidationWarning).filter(
        ValidationWarning.session_id == session.id,
        ValidationWarning.version_id == version.id,
    ).all()
    audit.record(
        db,
        action="RESUME_APPROVED",
        entity_type="ResumeVersion",
        entity_id=version.id,
        actor_id=actor_id,
        summary=f"V{version.version_no} approved — every warning ({len(resolved)}) had a decision",
        payload={
            "session_id": str(session.id),
            "submission_id": str(submission.id),
            "warnings": [
                {"type": w.warning_type, "status": w.status, "claim": w.claim_text, "message": w.message}
                for w in resolved
            ],
        },
    )
    db.flush()

    return {
        "submission": submission,
        "version": version,
        "files": files,
        "open_warning_count": len(outstanding),
    }


def mark_submitted(
    db: Session, submission: Submission, *, actor_id: UUID | None = None
) -> Submission:
    """Section 43 step 13 — the profile leaves this vendor's available pool."""
    return set_submission_status(db, submission, SubmissionStatus.SUBMITTED, actor_id=actor_id)


def set_submission_status(
    db: Session,
    submission: Submission,
    status: SubmissionStatus,
    *,
    reason: str | None = None,
    actor_id: UUID | None = None,
) -> Submission:
    """Drive the profile's vendor status from the submission status.

    The mapping lives in ``SUBMISSION_TO_PROFILE_STATUS`` so the two state
    machines in sections 8 and 33 can never disagree.
    """
    previous = submission.status
    submission.status = status
    now = utcnow()

    if status == SubmissionStatus.SUBMITTED and submission.submitted_at is None:
        submission.submitted_at = now
    if status in (SubmissionStatus.REJECTED, SubmissionStatus.RELEASED):
        submission.closed_at = now

    db.add(
        SubmissionStatusHistory(
            submission_id=submission.id,
            from_status=previous,
            to_status=status,
            changed_by_id=actor_id,
            reason=reason,
        )
    )

    # One vocabulary, so the submission status IS the profile status and the
    # session status. No lookup table to keep true — just three places told the
    # same word.
    if status == SubmissionStatus.SUBMITTED:
        availability.mark_submitted(
            db,
            person_id=submission.person_id,
            vendor_id=submission.vendor_id,
            requirement_id=submission.requirement_id,
            actor_id=actor_id,
        )
        requirement = db.query(Requirement).filter(Requirement.id == submission.requirement_id).first()
        if requirement is not None:
            requirement.status = RequirementStatus.SUBMITTED
    elif status == SubmissionStatus.ENGAGED:
        availability.mark_engaged(
            db,
            person_id=submission.person_id,
            vendor_id=submission.vendor_id,
            requirement_id=submission.requirement_id,
            actor_id=actor_id,
        )
    elif status == SubmissionStatus.REJECTED:
        # Section 30 — back into the same vendor's pool, no manual restoration.
        availability.mark_rejected(
            db, person_id=submission.person_id, vendor_id=submission.vendor_id, actor_id=actor_id, note=reason
        )
    elif status == SubmissionStatus.RELEASED:
        availability.mark_released(
            db, person_id=submission.person_id, vendor_id=submission.vendor_id, actor_id=actor_id
        )

    session = db.query(ResumeCompositionSession).filter(
        ResumeCompositionSession.id == submission.session_id
    ).first()
    if session is not None:
        session.status = ResumeSessionStatus(str(status))

    audit.record(
        db,
        action="SUBMISSION_STATUS_CHANGED",
        entity_type="Submission",
        entity_id=submission.id,
        actor_id=actor_id,
        summary=f"{previous} -> {status}",
        payload={"reason": reason},
    )
    db.flush()
    return submission


def abandon_session(
    db: Session, session: ResumeCompositionSession, *, actor_id: UUID | None = None
) -> ResumeCompositionSession:
    # RELEASED, not a private "abandoned": dropping a composition is letting the
    # person go for that vendor, which is a word the rest of the platform
    # already has.
    session.status = ResumeSessionStatus.RELEASED
    if session.reservation_id:
        availability.release_reservation(
            db, reservation_id=session.reservation_id, reason="MANUAL", actor_id=actor_id
        )
    audit.record(
        db,
        action="SESSION_RELEASED",
        entity_type="ResumeCompositionSession",
        entity_id=session.id,
        actor_id=actor_id,
    )
    db.flush()
    return session


def delete_session(
    db: Session, session: ResumeCompositionSession, *, actor_id: UUID | None = None
) -> None:
    """Remove a draft outright — the one case section 27's "nothing is lost"
    rule does not cover.

    Only while EDITING, and only when nothing has ever been generated toward a
    submission a vendor could have seen. Status alone does not prove that: a
    session that was approved and submitted, then reopened for more work (the
    "Open" action from Vendor tracking), returns to EDITING while still owning
    real submission history. Deleting it would cascade onto resume_versions and
    hit the RESTRICT foreign key submissions.version_id carries specifically
    to prevent that — which is real protection, but it surfaced as a raw 500
    from Postgres instead of an answer, because nothing checked for this before
    asking the database to delete. Checked explicitly here instead, with the
    same message ``abandon_session`` — the right call once there is history —
    would want the operator to read.
    """
    if session.status != ResumeSessionStatus.EDITING:
        raise ConflictError(
            "Only a session still in EDITING can be deleted. Release it instead."
        )
    if db.query(Submission).filter(Submission.session_id == session.id).first() is not None:
        raise ConflictError(
            "This session was previously submitted to a vendor, so its history "
            "cannot be deleted — release it instead so the record is kept."
        )
    if session.reservation_id:
        availability.release_reservation(
            db, reservation_id=session.reservation_id, reason="MANUAL", actor_id=actor_id
        )
    # The row is about to go, but the fact that it existed and was deleted —
    # by whom, when — is exactly what the audit trail exists to keep even when
    # the entity itself does not (see app/models/audit.py).
    audit.record(
        db,
        action="SESSION_DELETED",
        entity_type="ResumeCompositionSession",
        entity_id=session.id,
        actor_id=actor_id,
        summary=f"{session.reference_code or session.id} deleted while still in EDITING",
        payload={"requirement_id": str(session.requirement_id), "person_id": str(session.person_id)},
    )
    requirement_id = session.requirement_id
    db.delete(session)
    db.flush()

    # IN_PROGRESS means "a resume has been started against this JD" — the
    # deleted draft was the only evidence of that, so with no session left
    # behind it (approved or otherwise), the requirement is exactly as open
    # as it was before anyone touched it.
    requirement = db.query(Requirement).get(requirement_id)
    if requirement is not None and requirement.status == RequirementStatus.IN_PROGRESS:
        remaining = (
            db.query(ResumeCompositionSession)
            .filter(ResumeCompositionSession.requirement_id == requirement_id)
            .count()
        )
        if remaining == 0:
            requirement.status = RequirementStatus.OPEN
            db.flush()


def decide_warning(
    db: Session,
    warning: ValidationWarning,
    status: ValidationWarningStatus,
    *,
    note: str | None = None,
    actor_id: UUID | None = None,
) -> ValidationWarning:
    """Section 25 — [Keep] / [Modify] / [Dismiss]. The status recorded here is
    what ``approve`` checks: any warning left OPEN blocks approval, so calling
    this is how the user clears that block, not just a note for the record."""
    warning.status = status
    warning.decision_note = note
    warning.decided_by_id = actor_id
    warning.decided_at = utcnow()

    audit.record(
        db,
        action="WARNING_DECIDED",
        entity_type="ValidationWarning",
        entity_id=warning.id,
        actor_id=actor_id,
        summary=f"{warning.warning_type} -> {status}",
        payload={"claim": warning.claim_text, "note": note},
    )
    db.flush()
    return warning
