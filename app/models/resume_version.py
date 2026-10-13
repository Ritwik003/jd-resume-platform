"""Section 27 — every meaningful generation/edit cycle creates a version."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType
from app.utils.enums import ResumeVersionStatus


class ResumeVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_versions"
    __table_args__ = (UniqueConstraint("session_id", "version_no", name="uq_session_version_no"),)

    session_id = Column(
        GUID(), ForeignKey("resume_composition_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no = Column(Integer, nullable=False)

    #: The structured resume. Single source of truth for preview, DOCX and PDF.
    content_json = Column(JSONType(), nullable=False)
    #: The composed .tex for this version (section 26 templates are LaTeX).
    #: Kept so the PDF can be rebuilt later — once a LaTeX engine is installed —
    #: without paying for the model again, and so a failed compile can be
    #: debugged against the exact source that failed.
    latex_source = Column(Text, nullable=True)
    change_summary = Column(Text, nullable=True)
    #: BASE_GENERATION | AI_EDIT | MANUAL_EDIT | SELECTION_CHANGE | APPROVAL
    change_origin = Column(String(30), nullable=True)
    status = Column(String(20), nullable=False, default=ResumeVersionStatus.DRAFT)
    created_by_id = Column(GUID(), ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True)

    session = relationship("ResumeCompositionSession", back_populates="versions")
    files = relationship("ResumeFile", back_populates="version", cascade="all, delete-orphan")
    warnings = relationship("ValidationWarning", back_populates="version")

    def __repr__(self) -> str:
        return f"<ResumeVersion V{self.version_no}>"


class ResumeFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Section 27 — the DOCX and PDF artefacts belonging to a version."""

    __tablename__ = "resume_files"
    __table_args__ = (UniqueConstraint("version_id", "file_format", name="uq_version_format"),)

    version_id = Column(GUID(), ForeignKey("resume_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    file_format = Column(String(10), nullable=False)  # TEX | PDF | DOCX
    file_name = Column(String(500), nullable=False)
    storage_path = Column(String(1000), nullable=False)
    size_bytes = Column(BigInteger, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)

    version = relationship("ResumeVersion", back_populates="files")
