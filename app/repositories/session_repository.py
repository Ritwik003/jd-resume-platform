"""Reads for the resume composition session and its selection tables (section 20).

The three selection tables are always read in display order — that order is the
resume's own section order — so it belongs here rather than being remembered at
each call site.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.resume_session import (
    ResumeCompositionSession,
    ResumeSelectedCompany,
    ResumeSelectedProject,
    ResumeSelectedTechnology,
)
from app.models.resume_version import ResumeFile, ResumeVersion
from app.models.technology import Technology


# ────────────────────────────────────────────────────────────────── session
def get(db: Session, session_id: UUID) -> ResumeCompositionSession | None:
    return (
        db.query(ResumeCompositionSession)
        .filter(ResumeCompositionSession.id == session_id)
        .first()
    )


def count(db: Session) -> int:
    """Drives the next reference code (RS-00001, RS-00002, ...)."""
    return db.query(ResumeCompositionSession).count()


# ────────────────────────────────────────────────────────────────── version
def get_version(db: Session, version_id: UUID) -> ResumeVersion | None:
    return db.query(ResumeVersion).filter(ResumeVersion.id == version_id).first()


def version_in_session(
    db: Session, session_id: UUID, version_id: UUID
) -> ResumeVersion | None:
    """Scoped lookup: a version id from another session must not resolve."""
    return (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.session_id == session_id)
        .first()
    )


def versions_for_session(db: Session, session_id: UUID) -> list[ResumeVersion]:
    return (
        db.query(ResumeVersion)
        .filter(ResumeVersion.session_id == session_id)
        .order_by(ResumeVersion.version_no)
        .all()
    )


def delete_files_for_version(db: Session, version_id: UUID, file_format: str | None = None) -> None:
    query = db.query(ResumeFile).filter(ResumeFile.version_id == version_id)
    if file_format is not None:
        query = query.filter(ResumeFile.file_format == file_format)
    query.delete()


# ─────────────────────────────────────────────────────────────── selections
def selected_projects(db: Session, session_id: UUID) -> list[ResumeSelectedProject]:
    return (
        db.query(ResumeSelectedProject)
        .filter(ResumeSelectedProject.session_id == session_id)
        .order_by(ResumeSelectedProject.display_order)
        .all()
    )


def selected_companies(db: Session, session_id: UUID) -> list[ResumeSelectedCompany]:
    return (
        db.query(ResumeSelectedCompany)
        .filter(ResumeSelectedCompany.session_id == session_id)
        .order_by(ResumeSelectedCompany.display_order)
        .all()
    )


def selected_technologies(
    db: Session, session_id: UUID
) -> list[tuple[ResumeSelectedTechnology, Technology]]:
    return (
        db.query(ResumeSelectedTechnology, Technology)
        .join(Technology, Technology.id == ResumeSelectedTechnology.technology_id)
        .filter(ResumeSelectedTechnology.session_id == session_id)
        .order_by(ResumeSelectedTechnology.display_order)
        .all()
    )


def clear_selected_projects(db: Session, session_id: UUID) -> None:
    db.query(ResumeSelectedProject).filter(
        ResumeSelectedProject.session_id == session_id
    ).delete()


def clear_selected_companies(db: Session, session_id: UUID) -> None:
    db.query(ResumeSelectedCompany).filter(
        ResumeSelectedCompany.session_id == session_id
    ).delete()


def clear_selected_technologies(db: Session, session_id: UUID) -> None:
    db.query(ResumeSelectedTechnology).filter(
        ResumeSelectedTechnology.session_id == session_id
    ).delete()
