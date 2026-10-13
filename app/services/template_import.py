r"""Turn an uploaded PDF or Word resume into a LaTeX template.

Why this exists
---------------
A PDF is a rendered *output*, not a template. It holds positioned glyphs, not
sections — there is nothing to substitute, and nothing reflows. Drop a longer
name or a fourth project into it and the layout breaks. So "upload a PDF
template" cannot mean "fill this PDF"; it has to mean something else.

What it means here: the PDF is read as a **design specification**, and converted
once — at upload — into a self-contained ``.tex`` that reproduces that design.
From that moment the template is a LaTeX template like any other, and the entire
downstream pipeline runs unchanged: preamble copied verbatim, body generated,
Tectonic compiles, repair pass on failure.

Three things fall out of doing it at upload rather than at render:

* The derived ``.tex`` is self-contained by construction, so it can never hit
  the missing-``\input`` problem a hand-uploaded Overleaf main file hits.
* The conversion is paid for once, not on every resume.
* It is verified immediately — the derived template must compile before the
  upload is accepted, so a bad conversion fails in front of the person who
  uploaded it rather than three screens later.

What it is not: a pixel-exact copy. It reconstructs structure, typography,
spacing and rules. Fidelity is good, not perfect, and that is the honest ceiling
for anything that has to reflow around content of a different length.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path

from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS
from app.prompts.templates import TEMPLATE_FROM_DOCUMENT_SYSTEM, TEMPLATE_FROM_DOCUMENT_USER
from app.services.latex_composer import clean_latex, drop_missing_dependencies, split_document
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry
from app.services.rendering import latex
from app.utils.exceptions import ValidationError

logger = logging.getLogger(__name__)

#: Enough of a resume to convey the design; beyond this is repetition.
MAX_SOURCE_CHARS = 20_000

#: A conversion that comes back unusable is usually a bad sample, not a bad
#: prompt — the same retry the composer uses.
CONVERSION_ATTEMPTS = 3

#: Rebuilding a design means reasoning about layout and THEN writing a whole
#: preamble plus body. A reasoning model drafts inside <think> first, so the
#: ceiling has to cover both passes; at 14k it ran out mid-thought and returned
#: a reply with no answer in it.
CONVERSION_TOKENS = 40000


# ────────────────────────────────────────────────────── reading the upload
def _pdf_layout_text(payload: bytes) -> tuple[str, str]:
    r"""Page text with spacing preserved, plus a geometry note.

    ``extraction_mode="layout"`` keeps the horizontal positions, which is what
    tells the model whether the design is one column or two, where the rules
    sit, and how the header is aligned. Plain extraction throws exactly that
    away and produces a template that reads like a list.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001 — older pypdf, or an odd page tree
            pages.append(page.extract_text() or "")

    box = reader.pages[0].mediabox if reader.pages else None
    if box is not None:
        width_mm = round(float(box.width) * 25.4 / 72)
        height_mm = round(float(box.height) * 25.4 / 72)
        paper = "a4paper" if abs(width_mm - 210) <= 4 else "letterpaper"
        geometry = (
            f"{len(reader.pages)} page(s), {width_mm}x{height_mm} mm, "
            f"closest LaTeX class option: {paper}"
        )
    else:
        geometry = f"{len(reader.pages)} page(s), size unknown"

    return "\n\n".join(pages), geometry


def _docx_text(payload: bytes) -> tuple[str, str]:
    """Word documents already carry structure; keep paragraph styles as hints."""
    from docx import Document

    document = Document(io.BytesIO(payload))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (paragraph.style.name if paragraph.style else "") or ""
        lines.append(f"[{style}] {text}" if style and style != "Normal" else text)
    return "\n".join(lines), "Word document, A4 assumed"


def read_design_source(filename: str, payload: bytes) -> tuple[str, str]:
    """``(text, geometry_note)`` for whichever document format was uploaded."""
    suffix = Path(filename or "").suffix.lower()
    try:
        if suffix == ".pdf":
            text, geometry = _pdf_layout_text(payload)
        elif suffix == ".docx":
            text, geometry = _docx_text(payload)
        else:
            raise ValidationError(f"Cannot read a design from '{suffix or filename}'.")
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 — a corrupt upload must not 500
        raise ValidationError(f"Could not read that file: {exc}") from exc

    if not text.strip():
        raise ValidationError(
            "No text could be read from that file. A scanned or image-only resume "
            "has no text layer to learn the design from — upload a text-based PDF, "
            "a .docx, or a .tex."
        )
    return text[:MAX_SOURCE_CHARS], geometry


# ───────────────────────────────────────────────────────────── conversion
def _call(user: str) -> tuple[str, str | None]:
    provider = get_llm_provider()

    def _run():
        return provider.chat(
            [{"role": "user", "content": user}],
            model=LLM_MODEL,
            system=TEMPLATE_FROM_DOCUMENT_SYSTEM,
            temperature=LLM_TEMPERATURE,
            max_tokens=max(LLM_MAX_TOKENS, CONVERSION_TOKENS),
            timeout=max(LLM_TIMEOUT_SECONDS, 300.0),
        )

    try:
        response = with_retry(_run, max_retries=2)
    except (LLMTransientError, LLMPermanentError) as exc:
        raise ValidationError(
            f"The template could not be converted to LaTeX: {exc}"
        ) from exc
    return response.text, response.model


def _usable(source: str) -> bool:
    """A conversion is only usable if it is a whole, splittable document."""
    if r"\documentclass" not in source or r"\end{document}" not in source:
        return False
    parts = split_document(source)
    return parts is not None and bool(parts.body.strip())


def derive_latex(filename: str, payload: bytes) -> tuple[str, str | None]:
    """Convert an uploaded PDF/DOCX design into a self-contained ``.tex``.

    Returns ``(latex_source, model)``. Raises ``ValidationError`` when no usable
    template could be produced, so the upload fails rather than registering a
    template that breaks on first use.
    """
    text, geometry = read_design_source(filename, payload)
    user = TEMPLATE_FROM_DOCUMENT_USER.format(
        geometry=geometry,
        source_format=Path(filename or "").suffix.lstrip(".").upper() or "document",
        document=text,
    )

    source = ""
    model: str | None = None
    for attempt in range(CONVERSION_ATTEMPTS):
        started = time.perf_counter()
        raw, model = _call(user)
        elapsed = time.perf_counter() - started
        source = drop_missing_dependencies(clean_latex(raw))
        if _usable(source):
            logger.info(
                "Template conversion: %.1fs, %d chars in, %d chars of LaTeX out (%s)",
                elapsed,
                len(text),
                len(source),
                model,
            )
            break
        logger.warning(
            "Template conversion unusable after %.1fs (attempt %d/%d): %d chars",
            elapsed,
            attempt + 1,
            CONVERSION_ATTEMPTS,
            len(source),
        )
    else:
        raise ValidationError(
            "The uploaded design could not be turned into a usable LaTeX template. "
            "Try a text-based PDF with clear section headings, or upload a .tex directly."
        )
    return source, model


def verify_compiles(source: str) -> str | None:
    """Compile the derived template once. Returns an error summary, or ``None``.

    A conversion that does not compile is worthless, and finding that out at
    upload — while the person who chose the file is still looking at it — is far
    better than finding out when they generate their first resume from it.
    Returns rather than raises so the caller decides whether it is fatal: with
    no LaTeX engine installed there is nothing to verify against, and that must
    not block an upload.
    """
    if not latex.available():
        logger.info("No LaTeX engine; skipping template verification")
        return None
    import tempfile

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="jdresume-tplcheck-") as tmp:
        try:
            latex.compile_pdf(source, Path(tmp) / "check.pdf")
            # Worth logging even on success: the first compile with an unfamiliar
            # package set makes Tectonic fetch it, which can dominate the wait
            # and looks identical to the model being slow.
            logger.info("Template verified in %.1fs", time.perf_counter() - started)
            return None
        except latex.LatexCompileError as exc:
            logger.warning("Template failed to compile after %.1fs", time.perf_counter() - started)
            return str(exc)
