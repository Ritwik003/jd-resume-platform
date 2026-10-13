"""Resume composition enumerations."""

from __future__ import annotations

from enum import StrEnum


class ResumeSessionStatus(StrEnum):
    """Where one composition stands — the same words as everything else.

    A session mirrors the (person, vendor) hold it belongs to, so the History
    tab and the Vendor tracking module never describe the same situation two
    ways. See app/utils/enums/profile_status.py for the lifecycle.

    There is no AVAILABLE here: when a hold lapses the pair goes back to
    AVAILABLE but the session returns to EDITING, because the composition still
    exists and editing it takes the hold again.
    """

    EDITING = "EDITING"
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    ENGAGED = "ENGAGED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"

    @property
    def is_open(self) -> bool:
        """True while the session is still somebody's to finish."""
        return self in (ResumeSessionStatus.EDITING, ResumeSessionStatus.RESERVED)


class ResumeVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class ProficiencyLevel(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"
    EXPERT = "EXPERT"


class TechnologyTier(StrEnum):
    """Section 11 — primary vs secondary skills, per person profile."""

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


class SkillImportance(StrEnum):
    """Section 14 — importance belongs to the JD, never to the technology master."""

    MANDATORY = "MANDATORY"
    PREFERRED = "PREFERRED"
