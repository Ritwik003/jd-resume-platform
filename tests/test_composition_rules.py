"""The parts that decide what a generated resume asserts.

Three things are load-bearing enough to pin down:

* the fit score, because it chooses between "transform as-is" and "mould"
* the timeline expansion, because it changes dates a reader will total
* the composed project, because it puts work on a resume that has no row in
  the bank behind it
"""

from __future__ import annotations

from app.config import (
    RESUME_MAX_BULLETS_PER_PROJECT,
    RESUME_MAX_COMPANIES,
    RESUME_MAX_DESCRIPTION_CHARS,
    RESUME_MAX_PAGES,
    RESUME_MAX_PROJECTS,
    RESUME_MAX_SUMMARY_CHARS,
)
from app.schemas.resume_content import (
    EducationBlock,
    ExperienceBlock,
    ProjectBlock,
    ResumeContent,
    SkillGroup,
)
from app.schemas.sessions import SessionSelectionUpdate
from app.services import ground_rules
from app.services.jd_fit import role_similarity
from app.services.resume_tools import apply_tool


def _untouched_profile(db, *vendors):
    """A profile no test has already put in front of these vendors.

    The whole suite shares one seeded database (see tests/conftest.py, session
    scope), and any test that commits leaves (person, vendor) state behind it.
    So a test that opens with "this pair starts AVAILABLE" has to CHOOSE a pair
    that does, rather than taking whichever row the query happens to return.

    Taking the first row is what made this file fail about one run in ten, on a
    different test each time and never in isolation: usually the arbitrary row
    was clean, and occasionally it was somebody an earlier test had already
    engaged or released.
    """
    from app.models.availability import ProfileVendorStatus
    from app.models.person import PersonProfile

    spoken_for = {
        row.person_id
        for row in db.query(ProfileVendorStatus).filter(
            ProfileVendorStatus.vendor_id.in_([v.id for v in vendors]),
            ProfileVendorStatus.status != "AVAILABLE",
        )
    }
    query = db.query(PersonProfile)
    if spoken_for:
        query = query.filter(PersonProfile.person_id.notin_(spoken_for))
    profile = query.order_by(PersonProfile.id).first()
    assert profile is not None, (
        "every seeded profile is already spoken for by an earlier test — "
        "the seed needs more people than the suite consumes"
    )
    return profile


# ─────────────────────────────────────────────────────── role similarity
def test_same_role_scores_full_marks() -> None:
    assert role_similarity("Data Scientist", "Data Scientist") == 100


def test_seniority_and_parentheses_do_not_reduce_the_match() -> None:
    """"Senior Data Scientist (NLP)" is the same job as "Data Scientist"."""
    assert role_similarity("Data Scientist", "Senior Data Scientist (NLP)") == 100


def test_abbreviations_resolve() -> None:
    """Without synonyms this scores 0 and moulds a resume that needs no moulding."""
    assert role_similarity("ML Engineer", "Machine Learning Engineer") == 100


def test_different_disciplines_score_low() -> None:
    assert role_similarity("DevOps Engineer", "AI Engineer") < 60


# ──────────────────────────────────────────────────── house ground rules
def _content() -> ResumeContent:
    return ResumeContent(
        experience=[
            ExperienceBlock(company="Acme", designation="AI Engineer",
                            start_label="Jan 2024", end_label="Present"),
            ExperienceBlock(company="Beta", designation="Intern",
                            start_label="Jul 2023", end_label="Dec 2023"),
        ],
        projects=[
            ProjectBlock(name="Chatbot"),
            ProjectBlock(name="Intern tool"),
        ],
        education=[EducationBlock(qualification="B.Tech")],
        skill_groups=[SkillGroup(label="Core", items=["Python"])],
    )


def test_education_is_always_dropped() -> None:
    content = _content()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    assert content.education == []
    assert content.render_hints["education"] == "omitted"


def test_timeline_bars_are_switched_off() -> None:
    content = _content()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    assert content.render_hints["timeline_bars"] == "off"


def test_experience_expands_to_meet_the_jd() -> None:
    content = _content()
    before = ground_rules.current_experience_years(content)
    assert before is not None and before < 5

    ground_rules.apply_v1_ground_rules(content, target_experience_years=5)

    after = ground_rules.current_experience_years(content)
    assert after is not None and round(after) == 5
    assert content.render_hints["experience_years"] == 5.0


def test_expansion_adds_no_employers_and_leaves_no_gap() -> None:
    """The company list is the one fact the expansion may not touch."""
    content = _content()
    companies = [e.company for e in content.experience]

    ground_rules.apply_v1_ground_rules(content, target_experience_years=8)

    assert [e.company for e in content.experience] == companies
    # Roles are laid out contiguously: each start meets the previous end.
    newest, older = content.experience[0], content.experience[1]
    assert newest.start_label == older.end_label
    assert newest.end_label == "Present"


def test_an_already_long_enough_profile_is_left_alone() -> None:
    content = _content()
    content.experience[1].start_label = "Jan 2015"
    ground_rules.apply_v1_ground_rules(content, target_experience_years=3)
    assert content.experience[1].start_label == "Jan 2015"
    assert "experience_years" not in content.render_hints


# ──────────────────────────────────────────────────── composed projects
def test_a_project_can_be_composed_with_no_bank_row_behind_it() -> None:
    """Section 17 mix-and-match: the operator builds one directly, on this resume only."""
    content = _content()
    outcome = apply_tool(
        content,
        "add_project",
        {
            "name": "Kubernetes Platform Migration",
            "role": "DevOps Engineer",
            "technologies": ["Kubernetes", "Terraform"],
            "position": 0,
        },
    )
    assert outcome.applied
    added = content.projects[0]
    assert added.name == "Kubernetes Platform Migration"
    assert added.source_project_id is None
    assert added.technologies == ["Kubernetes", "Terraform"]


def test_the_same_project_is_never_added_twice() -> None:
    """The agent sees the conversation history and re-issues earlier calls."""
    content = _content()
    first = apply_tool(content, "add_project", {"name": "Platform Migration"})
    second = apply_tool(content, "add_project", {"name": "platform  migration"})

    assert first.applied
    assert not second.applied
    assert [p.name for p in content.projects].count("Platform Migration") == 1


# ─────────────────────────────────────────────────────── the one-page rule
def _overfull() -> ResumeContent:
    """More of everything than one page holds."""
    return ResumeContent(
        summary="S" * 900,
        experience=[
            ExperienceBlock(company=f"Co {i}", designation="Engineer",
                            start_label=f"Jan 201{i}", end_label=f"Dec 201{i}")
            for i in range(8)
        ],
        projects=[
            ProjectBlock(
                name=f"Project {i}",
                description="D" * 700,
                responsibilities=[f"resp {j}" for j in range(6)],
                achievements=[f"cut latency by {j}0%" for j in range(4)],
            )
            for i in range(8)
        ],
        skill_groups=[
            SkillGroup(label=f"Group {i}", items=[f"tech {j}" for j in range(20)])
            for i in range(9)
        ],
    )


def test_a_resume_is_cut_down_to_one_page() -> None:
    content = _overfull()
    result = ground_rules.apply_v1_ground_rules(content, target_experience_years=None)

    assert len(content.projects) == RESUME_MAX_PROJECTS
    assert len(content.experience) == RESUME_MAX_COMPANIES
    assert content.render_hints["max_pages"] == RESUME_MAX_PAGES
    assert len(content.summary) <= RESUME_MAX_SUMMARY_CHARS
    for project in content.projects:
        assert len(project.responsibilities) + len(project.achievements) <= RESUME_MAX_BULLETS_PER_PROJECT
        assert len(project.description) <= RESUME_MAX_DESCRIPTION_CHARS
    assert any("one-page" in line or "one page" in line for line in result.applied)


def test_the_projects_kept_are_the_ones_the_composition_ranked_first() -> None:
    """Ordering is decided against the JD before this runs; the trim takes the tail."""
    content = _overfull()
    leading = [p.name for p in content.projects[:RESUME_MAX_PROJECTS]]

    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)

    assert [p.name for p in content.projects] == leading


def test_a_project_keeps_one_metric_when_the_bullets_are_cut() -> None:
    """Achievements carry the numbers; a trim that drops them all keeps the weaker half."""
    content = _overfull()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    assert all(project.achievements for project in content.projects)


def test_trimming_twice_removes_nothing_the_second_time() -> None:
    """These rules re-run on every rebuild, so the trim has to settle."""
    content = _overfull()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    once = content.model_dump(mode="json")

    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)

    assert content.model_dump(mode="json") == once


def test_a_short_resume_is_left_alone() -> None:
    content = _content()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    assert len(content.projects) == 2
    assert len(content.experience) == 2


def test_the_expanded_timeline_spans_only_the_companies_that_print() -> None:
    """The trim runs first, so five employers absorb the JD's years, not eight."""
    content = _overfull()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=10)

    assert len(content.experience) == RESUME_MAX_COMPANIES
    span = ground_rules.current_experience_years(content)
    assert span is not None and round(span) == 10


def test_date_labels_end_up_in_one_format() -> None:
    """A studio edit may write "2024-03"; the rest of the page says "Mar 2024"."""
    content = _content()
    apply_tool(content, "set_experience", {"company_query": "Acme", "start_label": "2024-03"})

    ground_rules.normalise_date_labels(content)

    acme = next(e for e in content.experience if e.company == "Acme")
    assert acme.start_label == "Mar 2024"


def test_experience_dates_are_editable_from_the_studio() -> None:
    content = _content()
    outcome = apply_tool(
        content,
        "set_experience",
        {"company_query": "Acme", "start_label": "Jun 2021", "designation": "DevOps Engineer"},
    )
    assert outcome.applied
    acme = next(e for e in content.experience if e.company == "Acme")
    assert acme.start_label == "Jun 2021"
    assert acme.designation == "DevOps Engineer"


def test_education_can_be_put_back_on_request() -> None:
    """The house rule is a default, not a prohibition."""
    content = _content()
    ground_rules.apply_v1_ground_rules(content, target_experience_years=None)
    assert content.education == []

    outcome = apply_tool(content, "add_education", {"qualification": "B.Tech", "year": "2023"})
    assert outcome.applied
    assert len(content.education) == 1


# ────────────────────────────────────────────────── render + repair path
def test_a_failed_compile_repairs_without_duplicating_the_tex() -> None:
    r"""The repair pass rewrites the .tex, so it registers that format twice.

    The first ResumeFile may still be pending in the session, where a DELETE
    cannot reach it — both rows would then INSERT and violate the
    (version_id, file_format) unique constraint, taking down the whole render
    including the DOCX that had nothing to do with LaTeX.
    """
    import pytest

    from app.config.database import SessionLocal
    from app.models.person import Person
    from app.models.requirement import Requirement
    from app.models.resume_version import ResumeFile
    from app.models.vendor import Vendor
    from app.services import resume_session_service as svc
    from app.services.rendering import latex

    if not latex.available():
        pytest.skip("no LaTeX engine on this machine")

    with SessionLocal() as db:
        vendor = db.query(Vendor).order_by(Vendor.id).first()
        profile = _untouched_profile(db, vendor)
        person = db.query(Person).filter(Person.id == profile.person_id).first()

        requirement = Requirement(
            vendor_id=vendor.id,
            raw_jd_text="Backend Engineer\nRequired:\nPython\n",
            parsed_role="Backend Engineer",
        )
        db.add(requirement)
        db.flush()

        session = svc.create_session(
            db,
            requirement_id=requirement.id,
            person_id=person.id,
            person_profile_id=profile.id,
        )
        db.flush()
        version, _warnings, _composition = svc.generate_base_resume(db, session)

        # LaTeX that is syntactically doomed: \bogusmacro is defined nowhere,
        # so the first compile fails and the repair path runs.
        version.latex_source = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\bogusmacro{this command does not exist}\n"
            "\\end{document}\n"
        )
        db.query(ResumeFile).filter(ResumeFile.version_id == version.id).delete()
        db.flush()

        files = svc.render_files(db, session, version, ["TEX", "PDF", "DOCX"])
        db.flush()  # a constraint violation would surface here

        formats = [f.file_format for f in files]
        assert len(formats) == len(set(formats)), f"one row per format, got {formats}"
        assert "DOCX" in formats, "a LaTeX failure must not cost the DOCX"

        rows = (
            db.query(ResumeFile)
            .filter(ResumeFile.version_id == version.id, ResumeFile.file_format == "TEX")
            .count()
        )
        assert rows <= 1, "the repair pass must replace the .tex row, not add a second"
        db.rollback()


# ─────────────────────────────────── section 8: per-vendor availability
def test_an_unanswered_hold_expires_and_frees_the_profile() -> None:
    """The clock, not a person, is what ends a RESERVED hold nobody tended.

    An approved resume the vendor never answered would otherwise shelve someone
    indefinitely, so RESERVED carries a clock. The pair returns to AVAILABLE —
    nothing is lost, because the session goes back to EDITING and editing it
    takes a fresh hold.

    EDITING carries no clock at all. Even a stale ``reservation_expires_at`` on
    an EDITING row — the way one might be left over from before a status
    change, or forced here to prove the point — must not make the sweeper
    touch it: closing the tab is not a reason to let the person go.
    """
    from datetime import timedelta

    from app.config.database import SessionLocal
    from app.models.availability import ProfileVendorStatus
    from app.models.person import Person
    from app.models.vendor import Vendor
    from app.services import availability
    from app.utils.enums import ProfileVendorStatusEnum as S
    from app.utils.helper import utcnow

    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.id).first()
        person = db.query(Person).filter(Person.id == _untouched_profile(db, vendor).person_id).first()

        row = availability.get_or_create_status(db, person.id, vendor.id)
        row.status = S.RESERVED
        row.reservation_expires_at = utcnow() - timedelta(minutes=1)
        db.flush()

        assert availability.expire_stale_reservations(db, vendor_id=vendor.id) == 1
        db.flush()

        fresh = (
            db.query(ProfileVendorStatus)
            .filter(
                ProfileVendorStatus.person_id == person.id,
                ProfileVendorStatus.vendor_id == vendor.id,
            )
            .first()
        )
        assert str(fresh.status) == S.AVAILABLE, "RESERVED past its expiry must lapse"
        assert S(fresh.status).is_selectable, "a lapsed hold frees the profile"
        assert fresh.reservation_expires_at is None

        fresh.status = S.EDITING
        fresh.reservation_expires_at = utcnow() - timedelta(minutes=1)
        db.flush()

        assert availability.expire_stale_reservations(db, vendor_id=vendor.id) == 0
        db.flush()
        still_editing = (
            db.query(ProfileVendorStatus)
            .filter(
                ProfileVendorStatus.person_id == person.id,
                ProfileVendorStatus.vendor_id == vendor.id,
            )
            .first()
        )
        assert str(still_editing.status) == S.EDITING, "EDITING never lapses on its own"

        db.rollback()


def test_an_expired_session_revives_when_it_is_edited_again() -> None:
    """The loop: EDITING (no clock) -> RESERVED (clock) -> EXPIRED -> edit -> EDITING -> ...

    Selecting somebody does not start a clock; approving does. A RESERVED hold
    running out is not a verdict on the work. Editing a lapsed session takes
    the hold again — clock-free, same as a fresh selection — rather than
    refusing, and the session and the profile move together — History and
    Vendor tracking disagreeing is the failure this whole lifecycle exists to
    prevent.
    """
    from datetime import timedelta

    from app.config.database import SessionLocal
    from app.models.availability import ProfileReservation
    from app.models.requirement import Requirement
    from app.models.vendor import Vendor
    from app.services import availability, resume_session_service as svc
    from app.utils.enums import ProfileVendorStatusEnum as P
    from app.utils.enums import ResumeSessionStatus as R
    from app.utils.helper import utcnow

    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.id).first()
        profile = _untouched_profile(db, vendor)
        requirement = Requirement(
            vendor_id=vendor.id,
            raw_jd_text="Backend Engineer\nRequired:\nPython\n",
            parsed_role="Backend Engineer",
        )
        db.add(requirement)
        db.flush()

        session = svc.create_session(
            db,
            requirement_id=requirement.id,
            person_id=profile.person_id,
            person_profile_id=profile.id,
        )
        db.flush()

        def profile_state() -> str:
            return str(availability.get_or_create_status(db, profile.person_id, vendor.id).status)

        def lapse() -> None:
            """Wind the clock past the hold, the way an untended afternoon would."""
            row = availability.get_or_create_status(db, profile.person_id, vendor.id)
            row.reservation_expires_at = utcnow() - timedelta(minutes=1)
            for res in db.query(ProfileReservation).filter(
                ProfileReservation.person_id == profile.person_id,
                ProfileReservation.vendor_id == vendor.id,
                ProfileReservation.released_at.is_(None),
            ):
                res.expires_at = utcnow() - timedelta(minutes=1)
            db.flush()
            availability.expire_stale_reservations(db, vendor_id=vendor.id)
            db.flush()

        # An unapproved resume is one state, whether or not anyone has typed —
        # and it carries no clock, so nothing here has a deadline yet.
        svc.generate_base_resume(db, session)
        db.flush()
        assert str(session.status) == R.EDITING
        assert profile_state() == P.EDITING
        assert (
            availability.get_or_create_status(db, profile.person_id, vendor.id).reservation_expires_at
            is None
        ), "EDITING is open-ended — closing the tab must not start a timer"

        # Approval holds it for the vendor on a clock of its own. The SESSION
        # says RESERVED too — one vocabulary, three records.
        svc.approve(db, session, formats=["DOCX"])
        db.flush()
        assert str(session.status) == R.RESERVED
        assert profile_state() == P.RESERVED

        # Nobody said what happened, so that hold lapses...
        lapse()
        assert str(session.status) == R.EDITING
        assert profile_state() == P.AVAILABLE

        # ...and editing it takes the hold back, closing the loop.
        svc.update_selections(db, session, SessionSelectionUpdate(companies=[]))
        db.flush()
        assert str(session.status) == R.EDITING
        assert profile_state() == P.EDITING

        # Saying what happened moves all three records to the same word.
        svc.approve(db, session, formats=["DOCX"])
        db.flush()
        from app.models.submission import Submission
        from app.services import vendor_tracking

        vendor_tracking.set_pair_status(
            db, vendor_id=vendor.id, person_id=profile.person_id, status=P.SUBMITTED
        )
        db.flush()
        # This session was approved twice, so it has more than one submission.
        # Read the same one set_pair_status writes to -- the newest, ordered
        # exactly as the service orders it -- rather than an arbitrary row.
        submission = (
            db.query(Submission)
            .filter(Submission.session_id == session.id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .first()
        )
        assert profile_state() == P.SUBMITTED
        assert str(session.status) == R.SUBMITTED
        assert str(submission.status) == P.SUBMITTED

        db.rollback()


def test_a_lapsed_session_will_not_revive_over_someone_elses_hold() -> None:
    """Reviving must never hand the same person to two requirements at once."""
    import pytest

    from app.config.database import SessionLocal
    from app.models.requirement import Requirement
    from app.models.vendor import Vendor
    from app.services import availability, resume_session_service as svc
    from app.utils.enums import ProfileVendorStatusEnum as P
    from app.utils.exceptions import ReservationExpiredError

    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.id).first()
        profile = _untouched_profile(db, vendor)
        first = Requirement(vendor_id=vendor.id, raw_jd_text="A", parsed_role="Backend Engineer")
        second = Requirement(vendor_id=vendor.id, raw_jd_text="B", parsed_role="Data Engineer")
        db.add_all([first, second])
        db.flush()

        session = svc.create_session(
            db,
            requirement_id=first.id,
            person_id=profile.person_id,
            person_profile_id=profile.id,
        )
        db.flush()

        # The second requirement now holds this person for the same vendor.
        row = availability.get_or_create_status(db, profile.person_id, vendor.id)
        row.status = P.EDITING
        row.current_requirement_id = second.id
        db.flush()

        with pytest.raises(ReservationExpiredError):
            svc.update_selections(db, session, SessionSelectionUpdate(companies=[]))

        db.rollback()


def test_a_live_hold_is_left_alone_by_the_sweeper() -> None:
    """Only holds past their expiry lapse — the sweeper is not a reaper."""
    from datetime import timedelta

    from app.config.database import SessionLocal
    from app.models.person import Person
    from app.models.vendor import Vendor
    from app.services import availability
    from app.utils.enums import ProfileVendorStatusEnum as S
    from app.utils.helper import utcnow

    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.is_active.is_(True)).order_by(Vendor.id).first()
        person = db.query(Person).filter(Person.id == _untouched_profile(db, vendor).person_id).first()

        row = availability.get_or_create_status(db, person.id, vendor.id)
        row.status = S.RESERVED
        row.reservation_expires_at = utcnow() + timedelta(hours=2)
        db.flush()

        availability.expire_stale_reservations(db, vendor_id=vendor.id)
        db.flush()
        assert str(availability.get_or_create_status(db, person.id, vendor.id).status) == S.RESERVED

        db.rollback()


def test_the_profile_state_machine_runs_available_to_engaged_and_back() -> None:
    """AVAILABLE -> EDITING -> RESERVED -> SUBMITTED, then one of two branches:

    * REJECTED, which hands the person straight back to that vendor's pool
      (section 30) — a fresh opening, not a closed chapter.
    * ENGAGED -> RELEASED, a dead end for the ordinary flow: RELEASED only
      ever follows ENGAGED (you can only let go of somebody who was actually
      taken), and once released the profile never returns to that vendor's
      pool on its own — only a deliberate reopen brings it back.

    The chain is strict. ENGAGED and REJECTED are answers to a resume that
    went out, so neither is reachable from RESERVED: it must be SUBMITTED
    first.

    Availability is keyed on (person, vendor). The same person stays selectable
    for every other vendor at every step, which is the whole point of the rule —
    section 8 is explicit that it is not keyed on profile type.
    """
    import pytest

    from app.config.database import SessionLocal
    from app.models.requirement import Requirement
    from app.models.vendor import Vendor
    from app.services import availability, resume_session_service as svc
    from app.utils.enums import ProfileVendorStatusEnum as S
    from app.utils.exceptions import InvalidTransitionError

    with SessionLocal() as db:
        vendors = db.query(Vendor).filter(Vendor.is_active.is_(True)).limit(2).all()
        assert len(vendors) >= 2, "need two vendors to prove per-vendor isolation"
        first, second = vendors[0], vendors[1]
        profile = _untouched_profile(db, first, second)
        person_id = profile.person_id

        def state(vendor) -> str:
            row = availability.get_or_create_status(db, person_id, vendor.id)
            return str(row.status)

        def hold(vendor) -> None:
            """Select the profile for this vendor and approve, landing on RESERVED."""
            requirement = Requirement(
                vendor_id=vendor.id,
                raw_jd_text="Backend Engineer\nRequired:\nPython\n",
                parsed_role="Backend Engineer",
            )
            db.add(requirement)
            db.flush()
            session = svc.create_session(
                db,
                requirement_id=requirement.id,
                person_id=person_id,
                person_profile_id=profile.id,
            )
            db.flush()
            # Picking the profile opens a composition — a real lock, nothing
            # committed yet — which is the whole reason EDITING exists.
            assert state(vendor) == S.EDITING
            assert not S(state(vendor)).is_selectable
            svc.generate_base_resume(db, session)
            # Approving commits the resume to that vendor and starts the clock
            # on their answer. RESERVED, not ENGAGED: nobody has agreed yet.
            svc.approve(db, session, formats=["DOCX"])
            db.flush()

        # Default for any vendor.
        assert state(first) == S.AVAILABLE
        assert state(second) == S.AVAILABLE

        # --- branch 1: SUBMITTED -> REJECTED restores availability ---------
        hold(first)
        assert state(first) == S.RESERVED
        assert state(second) == S.AVAILABLE, "holding for one vendor must not touch another"
        assert not S(state(first)).is_selectable
        assert S(state(second)).is_selectable

        # An answer cannot arrive before the resume has gone out.
        with pytest.raises(InvalidTransitionError):
            availability.mark_rejected(db, person_id=person_id, vendor_id=first.id)
        with pytest.raises(InvalidTransitionError):
            availability.mark_engaged(db, person_id=person_id, vendor_id=first.id)

        availability.mark_submitted(db, person_id=person_id, vendor_id=first.id)
        db.flush()
        assert state(first) == S.SUBMITTED

        availability.mark_rejected(db, person_id=person_id, vendor_id=first.id)
        db.flush()
        assert state(first) == S.REJECTED
        assert S(state(first)).is_selectable, "section 30: rejection restores availability"

        # --- branch 2: SUBMITTED -> ENGAGED -> RELEASED is a dead end ------
        # A different vendor, so branch 1 leaving `first` REJECTED (and
        # selectable again) cannot be mistaken for this branch's behaviour.
        hold(second)
        assert state(second) == S.RESERVED

        # RELEASED is refused unless the pair is already ENGAGED.
        with pytest.raises(InvalidTransitionError):
            availability.mark_released(db, person_id=person_id, vendor_id=second.id)

        availability.mark_submitted(db, person_id=person_id, vendor_id=second.id)
        db.flush()
        assert state(second) == S.SUBMITTED

        availability.mark_engaged(db, person_id=person_id, vendor_id=second.id)
        db.flush()
        assert state(second) == S.ENGAGED
        assert not S(state(second)).is_selectable

        availability.mark_released(db, person_id=person_id, vendor_id=second.id)
        db.flush()
        assert state(second) == S.RELEASED
        assert not S(state(second)).is_selectable, \
            "a released profile must never return to this vendor's pool"

        # ...but a human may deliberately reopen it. That is the one way back,
        # and it is never automatic — see vendor_tracking.reopen_released_pair.
        from app.services import vendor_tracking

        vendor_tracking.reopen_released_pair(db, vendor_id=second.id, person_id=person_id)
        db.flush()
        assert state(second) == S.AVAILABLE
        assert S(state(second)).is_selectable

        db.rollback()
