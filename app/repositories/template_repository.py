"""Reads for the uploaded resume templates (section 26)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.resume_template import ResumeTemplate


def get(db: Session, template_id: UUID | None) -> ResumeTemplate | None:
    if template_id is None:
        return None
    return db.query(ResumeTemplate).filter(ResumeTemplate.id == template_id).first()


def list_active(db: Session) -> list[ResumeTemplate]:
    return db.query(ResumeTemplate).filter(ResumeTemplate.is_active.is_(True)).all()


def first_active(db: Session) -> ResumeTemplate | None:
    return db.query(ResumeTemplate).filter(ResumeTemplate.is_active.is_(True)).first()


def first_for_profile_type(db: Session, profile_type_id: UUID | None) -> ResumeTemplate | None:
    """A template earmarked for this role, if the operator configured one."""
    if profile_type_id is None:
        return None
    return (
        db.query(ResumeTemplate)
        .filter(
            ResumeTemplate.profile_type_id == profile_type_id,
            ResumeTemplate.is_active.is_(True),
        )
        .first()
    )
