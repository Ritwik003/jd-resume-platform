"""Sections 24/25 — validation warning taxonomy.

Only a handful of these types are raised while
``config.VALIDATION_STRICT_FACTS_ONLY`` is on, and each is a checkable fact —
two things that are both written down, compared:

    UNKNOWN_TECHNOLOGY             a skill name that matches nothing real
    EXPERIENCE_BELOW_REQUIREMENT   the stated total does not match the record
    MANDATORY_SKILL_MISSING        the JD asks for a skill the resume can't
                                    evidence, though the bank knows the skill
    MANDATORY_SKILL_NOT_IN_DATABASE the JD asks for a skill that matches no
                                    technology row at all — no resume drawn
                                    from this bank could ever evidence it
    NO_DIRECT_PROJECT_MATCH        nothing in the project bank shares the
                                    JD's role; the closest available projects
                                    were selected and moulded toward it instead
    NO_DIRECT_COMPANY_MATCH        nothing in the company bank shares the
                                    JD's role; the closest available employers
                                    were selected instead
    TIMELINE_ANACHRONISM           a technology named in a period that predates
                                    its release — the resume claims something
                                    that did not exist in the year given
    ROLE_ANACHRONISM               a job title dated before that title existed,
                                    e.g. "AI Engineer" in 2018
    EMPLOYER_WITHOUT_PROJECTS      an employer with nothing to show, because no
                                    project on record is old enough for its period
    UNGROUNDED_CLAIM               the moulding pass admitted it added something
                                    it could not ground (self-reported), or the
                                    groundedness judge found a stated metric,
                                    technology or outcome absent from the original
    MISDIRECTED_REFRAME            a project's moulded name/description describes
                                    a different system than the original recorded
                                    one, not the same system seen from another angle
    OVERCLAIMED_SCOPE              the wording inflates seniority, ownership or
                                    impact beyond what the original supports —
                                    fair representation, not fact-checking
    LANGUAGE_QUALITY_ISSUE         a grammar, spelling or clarity defect found
                                    on the final, post-edit resume at approval time

The rest are kept because historic warnings in the database still carry them and
must keep rendering, and because turning the flag off restores the wider set.
"""

from __future__ import annotations

from enum import StrEnum


class ValidationWarningType(StrEnum):
    # --- the strict, always-checkable set -----------------------------------
    UNKNOWN_TECHNOLOGY = "UNKNOWN_TECHNOLOGY"
    EXPERIENCE_BELOW_REQUIREMENT = "EXPERIENCE_BELOW_REQUIREMENT"
    MANDATORY_SKILL_MISSING = "MANDATORY_SKILL_MISSING"
    MANDATORY_SKILL_NOT_IN_DATABASE = "MANDATORY_SKILL_NOT_IN_DATABASE"
    NO_DIRECT_PROJECT_MATCH = "NO_DIRECT_PROJECT_MATCH"
    NO_DIRECT_COMPANY_MATCH = "NO_DIRECT_COMPANY_MATCH"
    TIMELINE_ANACHRONISM = "TIMELINE_ANACHRONISM"
    ROLE_ANACHRONISM = "ROLE_ANACHRONISM"
    EMPLOYER_WITHOUT_PROJECTS = "EMPLOYER_WITHOUT_PROJECTS"
    UNGROUNDED_CLAIM = "UNGROUNDED_CLAIM"
    MISDIRECTED_REFRAME = "MISDIRECTED_REFRAME"
    OVERCLAIMED_SCOPE = "OVERCLAIMED_SCOPE"
    LANGUAGE_QUALITY_ISSUE = "LANGUAGE_QUALITY_ISSUE"

    # --- retained for historic rows and for non-strict mode -----------------
    # TECHNOLOGY_PREDATES_PROJECT can no longer be raised: a project carries no
    # date of its own to check a technology's release date against.
    TECHNOLOGY_PREDATES_PROJECT = "TECHNOLOGY_PREDATES_PROJECT"
    ANACHRONISTIC_TERMINOLOGY = "ANACHRONISTIC_TERMINOLOGY"
    OVERLAPPING_EMPLOYMENT = "OVERLAPPING_EMPLOYMENT"
    PROJECT_OUTSIDE_EMPLOYMENT = "PROJECT_OUTSIDE_EMPLOYMENT"
    EXPERIENCE_TOTAL_MISMATCH = "EXPERIENCE_TOTAL_MISMATCH"
    TECHNOLOGY_NOT_IN_PROFILE = "TECHNOLOGY_NOT_IN_PROFILE"
    COMPANY_REATTRIBUTED = "COMPANY_REATTRIBUTED"
    TIMELINE_GAP = "TIMELINE_GAP"


class ValidationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ValidationWarningStatus(StrEnum):
    """Section 25 — the user decides what a warning means: keep the claim,
    modify it, or dismiss it as inapplicable. But approval itself now requires
    every warning on the current version to carry one of those decisions —
    OPEN is the only status that blocks it (see ``resume_session_service.approve``).
    """

    OPEN = "OPEN"
    KEPT = "KEPT"
    MODIFIED = "MODIFIED"
    DISMISSED = "DISMISSED"
