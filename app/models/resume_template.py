"""Section 26 — plug-and-play resume templates.

A template is a JSON *layout definition* (section order, visibility, style
tokens, page limits), never code and never a schema change. The same layout
definition drives the HTML preview, the DOCX and the PDF, so all three cannot
drift apart.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import GUID, JSONType


class ResumeTemplate(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "resume_templates"

    name = Column(String(255), nullable=False, unique=True, index=True)
    code = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    #: Optional hint only — any template may be used with any profile type.
    profile_type_id = Column(GUID(), ForeignKey("profile_types.id", ondelete="SET NULL"), nullable=True)
    layout_definition = Column(JSONType(), nullable=False, default=dict)
    version = Column(String(20), nullable=False, default="1")

    # Section 26 — a template is either a built-in JSON layout the platform
    # draws itself, or a file the organisation uploaded and we fill in.
    #: LAYOUT | DOCX | LATEX | PDF
    source_format = Column(String(10), nullable=False, default="LAYOUT")
    source_file_name = Column(String(500), nullable=True)
    source_storage_path = Column(String(1000), nullable=True)
    source_size_bytes = Column(BigInteger, nullable=True)
    #: Placeholders detected in an uploaded DOCX/LaTeX template.
    source_placeholders = Column(JSONType(), nullable=True)

    # A PDF or Word upload is a design, not a fillable template, so it is
    # converted to LaTeX once at upload and that conversion is stored here.
    # ``source_storage_path`` keeps pointing at the file the operator actually
    # uploaded, so they can still download what they gave us. Rendering reads
    # this column first and falls back to the file.
    derived_latex = Column(Text, nullable=True)
    #: Which model produced ``derived_latex``, for when a conversion looks wrong.
    derived_by_model = Column(String(100), nullable=True)

    # ── the blueprint pipeline (see app/services/template_blueprint.py) ──────
    #
    # An uploaded template is turned into two things ONCE, at upload:
    #
    #   blueprint     what sections this design has, in its own order and its
    #                 own words — the shape the content window then takes, so
    #                 every template edits as the design it actually is
    #   jinja_source  the same .tex with its worked example swapped for Jinja
    #                 loops over the resume's data
    #
    # After that, rendering is a deterministic Jinja render plus a compile —
    # no model writes LaTeX per resume any more, which is what made the same
    # content land differently in the same template on two consecutive runs.
    #: [{"key", "title", "kind", "enabled"}], in the template's own order.
    blueprint = Column(JSONType(), nullable=True)
    #: The .tex as a Jinja template. ``<< >>`` for values, ``<% %>`` for logic,
    #: so LaTeX's own braces never collide with Jinja's.
    jinja_source = Column(Text, nullable=True)
    #: Directory holding a multi-file upload (.cls, .sty, images) — the whole
    #: Overleaf project, compiled together the way Overleaf compiles it.
    project_dir = Column(String(1000), nullable=True)
    #: The file inside ``project_dir`` carrying \documentclass.
    main_tex_name = Column(String(500), nullable=True)

    profile_type = relationship("ProfileType")

    @property
    def is_fillable(self) -> bool:
        """Has this design been turned into a template that can be filled in?

        False means rendering still falls back to composing LaTeX per resume —
        slower, and not identical run to run. Surfaced on the API so an
        operator can see which of their templates need re-converting rather
        than discovering it from a resume that looks different.
        """
        return bool((self.jinja_source or "").strip())

    @property
    def section_titles(self) -> list[str]:
        """The design's own section headings, in its own order."""
        raw = self.blueprint if isinstance(self.blueprint, dict) else {}
        rows = raw.get("sections") if isinstance(raw.get("sections"), list) else []
        return [str(r.get("title") or "") for r in rows if isinstance(r, dict)]

    def __repr__(self) -> str:
        return f"<ResumeTemplate {self.code}>"
