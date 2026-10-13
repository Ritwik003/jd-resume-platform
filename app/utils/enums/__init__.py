"""Domain enumerations shared by models, schemas and services."""

from app.utils.enums.profile_status import MANUAL_TRANSITIONS, MANUALLY_SETTABLE, ProfileVendorStatusEnum
from app.utils.enums.requirement_status import RequirementStatus
from app.utils.enums.resume import (
    ProficiencyLevel,
    ResumeSessionStatus,
    ResumeVersionStatus,
    SkillImportance,
    TechnologyTier,
)
from app.utils.enums.submission_status import SubmissionStatus
from app.utils.enums.validation import ValidationSeverity, ValidationWarningStatus, ValidationWarningType

__all__ = [
    "MANUAL_TRANSITIONS",
    "MANUALLY_SETTABLE",
    "ProfileVendorStatusEnum",
    "RequirementStatus",
    "ProficiencyLevel",
    "ResumeSessionStatus",
    "ResumeVersionStatus",
    "SkillImportance",
    "TechnologyTier",
    "SubmissionStatus",
    "ValidationSeverity",
    "ValidationWarningStatus",
    "ValidationWarningType",
]
