"""/resume-templates — section 26 (plug-and-play templates)."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.resume_template import ResumeTemplate
from app.models.user import User
from app.repositories.base_repository import ensure_unique, get_or_404, list_and_count
from app.schemas.common import Page, PageMeta
from app.schemas.masters import ResumeTemplateCreate, ResumeTemplateResponse
from app.schemas.resume_content import ContactBlock, ResumeContent
from app.services import (
    latex_composer,
    storage,
    template_import,
    template_project,
    templatize,
)
from app.services import template_blueprint as tb
from app.services.rendering import latex
from app.services.rendering.docx_template import (
    BLOCK_TOKENS,
    SCALAR_TOKENS,
)
from app.services.rendering.layout import merge_layout
from app.utils.exceptions import NotFoundError, ValidationError
from app.utils.helper import slugify
from app.utils.pagination import Pagination, pagination_params

#: \VAR{token} / \placeholder{token} style tokens inside an uploaded .tex file.
_LATEX_TOKEN = re.compile(r"\\(?:VAR|placeholder)\{\s*([a-z0-9_]+)\s*\}", re.I)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=Page[ResumeTemplateResponse])
def list_templates(
    profile_type_id: UUID | None = Query(None),
    page: Pagination = Depends(pagination_params),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[ResumeTemplateResponse]:
    """Paginated like every other master collection, so clients share one shape."""
    query = db.query(ResumeTemplate).filter(ResumeTemplate.is_active.is_(True))
    if profile_type_id:
        query = query.filter(ResumeTemplate.profile_type_id == profile_type_id)
    items, total = list_and_count(query.order_by(ResumeTemplate.name), page.skip, page.limit)
    return Page(
        items=[ResumeTemplateResponse.model_validate(t) for t in items],
        meta=PageMeta(skip=page.skip, limit=page.limit, total=total),
    )


@router.post("", response_model=ResumeTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: ResumeTemplateCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ResumeTemplate:
    """Adding a template is an INSERT — no schema change, no code change."""
    ensure_unique(db, ResumeTemplate, "code", payload.code)
    ensure_unique(db, ResumeTemplate, "name", payload.name)
    template = ResumeTemplate(**payload.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.get("/{template_id}", response_model=ResumeTemplateResponse)
def get_template(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ResumeTemplate:
    return get_or_404(db, ResumeTemplate, template_id, "Resume template")


@router.get("/{template_id}/resolved-layout")
def resolved_layout(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict:
    """The layout after defaults are merged in — what the renderers actually use."""
    template = get_or_404(db, ResumeTemplate, template_id, "Resume template")
    return merge_layout(template.layout_definition)


# ─────────────────────────────────────────── section 26: uploaded templates
#: Everything renders through LaTeX, because that is the only one of the three
#: that can be recompiled around content of a different length.
#:
#: A .tex is used as-is. A .pdf or .docx is a rendered *design*, not a fillable
#: template — there is nothing to substitute and nothing reflows — so it is
#: converted to a self-contained .tex once, here at upload, and everything
#: downstream then treats it as an ordinary LaTeX template.
UPLOAD_FORMATS = {".tex": "LATEX", ".pdf": "PDF", ".docx": "DOCX", ".zip": "LATEX"}
#: The formats that arrive as a design and have to be converted before use.
DERIVED_FORMATS = {"PDF", "DOCX"}
#: The size limit lives with the unpacker, in template_project, because that is
#: what enforces it — a project's total unpacked size, not one file's.


def _prepare(template: ResumeTemplate, source: str, project: Path | None) -> str | None:
    r"""Read a design's sections and turn it into a fillable template. Once.

    Two artefacts come out and both are stored on the row:

    * ``blueprint`` — the sections this design actually has, in its own order
      and its own wording. The content window is built from it, so every
      template edits as the design it really is.
    * ``jinja_source`` — the same .tex with its worked example replaced by
      loops over the resume's data.

    Returns ``None`` on success, or the reason the templatize step failed.
    A failure is NOT fatal: the blueprint is still stored, and rendering falls
    back to the older per-resume composition. Refusing the upload outright
    would mean a provider outage locks an operator out of their own template.
    """
    blueprint = tb.detect_sections(source)
    template.blueprint = blueprint.to_dict()

    try:
        jinja_source, model = templatize.templatize(
            source,
            blueprint,
            name=template.name,
            assets_dir=str(project) if project else None,
        )
    except tb.TemplatizeError as exc:
        logger.warning("Could not templatize %s: %s", template.name, exc)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 — an upload must not 500 on this
        logger.exception("Templatizing %s failed", template.name)
        return str(exc)

    template.jinja_source = jinja_source
    template.derived_by_model = model or template.derived_by_model
    return None


def _project_dir(template: ResumeTemplate) -> Path | None:
    directory = (template.project_dir or "").strip()
    if not directory:
        return None
    path = storage.absolute_path(directory)
    return path if path.is_dir() else None


def _template_source(template: ResumeTemplate) -> str:
    """The design's LaTeX, flattened, from wherever this template keeps it."""
    project = _project_dir(template)
    if project is not None:
        main = project / (template.main_tex_name or "")
        if not main.is_file():
            main = template_project.find_main_tex(project)
        return template_project.flatten(main, project)
    return latex_composer.read_template_source(template)


@router.post("/upload", response_model=ResumeTemplateResponse, status_code=status.HTTP_201_CREATED)
async def upload_template(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    description: str | None = Form(None),
    profile_type_id: UUID | None = Form(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ResumeTemplate:
    r"""Register a template from an uploaded design — a file, or a whole project.

    Upload what Overleaf would hold: the main ``.tex`` plus the ``.cls``,
    ``.sty`` and image files that make it look the way it does, either as
    several files or as one ``.zip``. They are kept together in a project
    directory and compiled together, exactly as Overleaf compiles them, so
    what the operator saw there is what this prints.

    Two things happen here and only here, because they are the expensive half
    and doing them per resume is what made the same content land differently
    on two consecutive runs:

    * the design's **blueprint** is read — its sections, its order, its
      wording — and becomes the content window for every resume in it;
    * the design is **templatized** into Jinja, verified by rendering a
      realistic sample through it and compiling that to a PDF.

    A ``.pdf`` or ``.docx`` is a rendered design rather than a fillable
    source, so it is converted to LaTeX first and then treated identically.
    """
    # The uploaded parts are read off the form rather than declared as
    # parameters. Declaring both a repeated ``files`` and a single ``file``
    # made the framework treat the one that was absent as missing, which
    # rejected perfectly good uploads; and taking the form directly is also
    # what lets this accept either field name, so the older single-file
    # clients keep working unchanged.
    form = await request.form()
    uploads = [
        value
        for key, value in form.multi_items()
        if key in ("files", "file") and hasattr(value, "filename")
    ]
    if not uploads:
        raise ValidationError(
            "No file was uploaded. Send the template's .tex, or a .zip of the project."
        )

    payloads: list[tuple[str, bytes]] = []
    total = 0
    for upload in uploads:
        data = await upload.read()
        if not data:
            continue
        total += len(data)
        payloads.append((upload.filename or "upload.tex", data))
    if not payloads:
        raise ValidationError("The uploaded template file is empty.")
    if total > template_project.MAX_PROJECT_BYTES:
        raise ValidationError(
            f"That upload is {total // 1024 // 1024} MB; the limit is "
            f"{template_project.MAX_PROJECT_BYTES // 1024 // 1024} MB."
        )

    suffixes = {Path(n).suffix.lower() for n, _ in payloads}
    unknown = suffixes - set(UPLOAD_FORMATS) - template_project.ALLOWED_SUFFIXES
    if unknown:
        raise ValidationError(
            f"Unsupported template file type(s): {', '.join(sorted(unknown))}. "
            "A template is a .tex (with any .cls/.sty/images it needs), a .zip of "
            "those, or a .pdf/.docx design."
        )

    ensure_unique(db, ResumeTemplate, "code", code)
    ensure_unique(db, ResumeTemplate, "name", name)

    single = payloads[0] if len(payloads) == 1 else None
    single_suffix = Path(single[0]).suffix.lower() if single else ""
    fmt = UPLOAD_FORMATS.get(single_suffix, "LATEX") if single else "LATEX"

    project = storage.template_dir() / (slugify(code) or uuid4().hex)
    derived_latex: str | None = None
    derived_model: str | None = None
    placeholders: list[str] | None = None
    main_name: str | None = None

    try:
        if fmt in DERIVED_FORMATS:
            # A design, not a template: nothing to substitute and nothing
            # reflows, so it is converted to a self-contained .tex once, here.
            derived_latex, derived_model = template_import.derive_latex(*single)
            compile_error = template_import.verify_compiles(derived_latex)
            if compile_error:
                raise ValidationError(
                    "The design was converted but the result does not compile: "
                    f"{compile_error} Try a text-based PDF with clear section headings, "
                    "or upload the .tex directly."
                )
            project.mkdir(parents=True, exist_ok=True)
            stored = project / f"{slugify(code) or 'template'}{single_suffix}"
            stored.write_bytes(single[1])
            main_name = "main.tex"
            (project / main_name).write_text(derived_latex, encoding="utf-8")
            source = derived_latex
        else:
            template_project.unpack(payloads, project)
            template_project.assert_single_project(project)
            main = template_project.find_main_tex(project)
            main_name = str(main.relative_to(project)).replace("\\", "/")
            source = template_project.flatten(main, project)
            stored = main
            placeholders = sorted({m.group(1).lower() for m in _LATEX_TOKEN.finditer(source)}) or None
    except ValidationError:
        shutil.rmtree(project, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 — a corrupt upload must not 500
        shutil.rmtree(project, ignore_errors=True)
        raise ValidationError(f"Could not read that template: {exc}") from exc

    template = ResumeTemplate(
        name=name,
        code=code,
        description=description,
        profile_type_id=profile_type_id,
        layout_definition={},
        source_format=fmt,
        source_file_name=single[0] if single else main_name,
        source_storage_path=storage.relative_path(stored),
        source_size_bytes=total,
        source_placeholders=placeholders,
        derived_latex=derived_latex,
        derived_by_model=derived_model,
        project_dir=storage.relative_path(project),
        main_tex_name=main_name,
    )

    problem = _prepare(template, source, project)
    if problem:
        # Kept, but say so: the operator can retry from /retemplatize once the
        # provider is healthy, and until then this template still renders
        # through the older per-resume path.
        template.description = (
            f"{description + ' - ' if description else ''}Not yet fillable: {problem[:300]}"
        )

    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.post("/{template_id}/retemplatize", response_model=ResumeTemplateResponse)
def retemplatize(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ResumeTemplate:
    """Re-read a template's sections and rebuild its Jinja source.

    For the two cases that actually happen: the conversion failed at upload
    because the provider was down, and the design was edited on disk.
    """
    template = get_or_404(db, ResumeTemplate, template_id, "Resume template")
    problem = _prepare(template, _template_source(template), _project_dir(template))
    if problem:
        raise ValidationError(f"'{template.name}' could not be prepared: {problem}")
    db.commit()
    db.refresh(template)
    return template


@router.get("/{template_id}/blueprint")
def read_blueprint(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict:
    """The sections this design has — the shape its content window takes.

    Read this to show an operator what they are choosing BEFORE a session
    exists: which sections the template carries, in its order and its own
    wording, and whether it is fillable yet.
    """
    template = get_or_404(db, ResumeTemplate, template_id, "Resume template")
    blueprint = tb.Blueprint.from_dict(template.blueprint)
    return {
        "template_id": str(template.id),
        "name": template.name,
        "is_fillable": bool((template.jinja_source or "").strip()),
        "sections": [s.to_dict() for s in blueprint.sections],
    }


@router.get("/{template_id}/source")
def download_template_source(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Download the originally uploaded template file."""
    template = get_or_404(db, ResumeTemplate, template_id)
    if not template.source_storage_path:
        raise NotFoundError("This template is a built-in layout and has no uploaded file.")
    path = storage.absolute_path(template.source_storage_path)
    if not path.exists():
        raise NotFoundError("The stored template file is missing from disk.")
    return FileResponse(path, filename=template.source_file_name or path.name)


@router.get("/{template_id}/preview")
def preview_template(
    template_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Compile this template into a PDF of its design — the Overleaf view.

    A templatized design is filled with a realistic sample candidate and
    compiled beside its own .cls/.sty/images, so the operator sees the design
    working with a whole resume in it: every section, every loop, the real
    spacing. That is the thing somebody choosing a template is choosing.

    A template that has not been templatized yet falls back to compiling the
    worked example it shipped with, which is still its design and still
    instant.
    """
    template = get_or_404(db, ResumeTemplate, template_id)
    project = _project_dir(template)
    assets = str(project) if project else None

    if (template.jinja_source or "").strip():
        document = latex_composer.render_blueprint(template, templatize.sample_content())
    else:
        source = _template_source(template)
        sample = ResumeContent(
            contact=ContactBlock(
                full_name="Candidate Name",
                headline="Target Role",
                email="name@example.com",
                phone="+91 00000 00000",
                location="City, Country",
            )
        )
        parts = latex_composer.split_document(source)
        if parts is None:
            document = source
        else:
            preamble = latex_composer.fill_preamble_placeholders(parts.preamble, sample)
            if assets is None:
                # Nothing on disk to resolve a missing \usepackage against, so
                # the ones this machine does not have are dropped rather than
                # failing the preview.
                preamble = latex_composer.drop_missing_dependencies(preamble)
            document = f"{preamble}\n{parts.body}\n\\end{{document}}\n"

    out = storage.absolute_path("previews") / f"{template.code}-preview.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        latex.compile_pdf(document, out, assets_dir=assets)
    except latex.LatexCompileError as exc:
        raise ValidationError(
            f"'{template.name}' does not compile, so it has no preview. {exc}"
        ) from exc
    return FileResponse(out, media_type="application/pdf", filename=f"{template.code}.pdf")


@router.get("/placeholders/reference", response_model=dict)
def placeholder_reference(_: User = Depends(get_current_user)) -> dict:
    """The placeholder vocabulary to use when authoring a Word template."""
    return {
        "scalar": sorted(SCALAR_TOKENS),
        "blocks": sorted(BLOCK_TOKENS),
        "usage": {
            "scalar": "{{full_name}} — replaced in place, keeps your font and styling",
            "block": "{{#projects}} — the paragraph is replaced by one group per project",
        },
    }
