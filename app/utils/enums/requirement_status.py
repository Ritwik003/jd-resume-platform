"""Section 32 — Requirement lifecycle."""

from __future__ import annotations

from enum import StrEnum


class RequirementStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
