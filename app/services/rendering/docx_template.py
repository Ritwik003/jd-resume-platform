"""Section 26 — render a resume into an *uploaded* .docx template.

The built-in templates are JSON layout definitions, which the platform draws
from scratch. An uploaded Word file is the opposite: the organisation already
owns the design, and we only fill it in. This module does that fill.

Two placeholder shapes are supported, both written directly in Word:

    {{full_name}}          a scalar — replaced in place, keeping the run's
                           font, size, colour and weight
    {{#projects}}          a block — the paragraph is deleted and replaced by
                           generated paragraphs, one group per item

Word habitually splits a typed ``{{name}}`` across several runs (it does this
whenever spell-check, formatting or an autocorrect boundary intervenes), so a
naive run-by-run replace misses most placeholders. Every routine here works on
the joined paragraph text and then rewrites the runs, which is why it survives
real documents produced by real users.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document

from app.schemas.resume_content import ResumeContent

#: ``{{ token }}`` / ``{{#token}}`` — whitespace tolerant, case-insensitive.
_TOKEN = re.compile(r"\{\{\s*#?\s*([a-z0-9_]+)\s*\}\}", re.I)
_BLOCK = re.compile(r"\{\{\s*#\s*([a-z0-9_]+)\s*\}\}", re.I)

#: Block placeholders expand into many paragraphs; everything else is scalar.
BLOCK_TOKENS = {"skills", "experience", "projects", "education", "certifications"}

SCALAR_TOKENS = {
    "full_name", "headline", "email", "phone", "location", "links", "summary",
    "vendor", "role", "total_experience",
}


# ───────────────────────────────────────────────────────────── inspection
def detect_placeholders(path: str | Path) -> list[str]:
    """Return every placeholder token found in the document, in order."""
    doc = Document(str(path))
    seen: list[str] = []

    def scan(text: str) -> None:
        for m in _TOKEN.finditer(text or ""):
            tok = m.group(1).lower()
            if tok not in seen:
                seen.append(tok)

    for p in doc.paragraphs:
        scan(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    scan(p.text)
    for section in doc.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                scan(p.text)
    return seen


# ───────────────────────────────────────────────────────────── scalars
def _scalar_values(content: ResumeContent, extra: dict[str, Any]) -> dict[str, str]:
    c = content.contact
    # No career total is stored on a person. Where an uploaded template asks for
    # one, it gets the figure this resume actually asserts — the JD's, recorded
    # by the ground rule that laid out the timeline.
    years = extra.get("total_experience") or content.render_hints.get("experience_years")
    return {
        "full_name": c.full_name or "",
        "headline": c.headline or "",
        "email": c.email or "",
        "phone": c.phone or "",
        "location": c.location or "",
        "links": ", ".join(c.links or []) if isinstance(c.links, list) else (c.links or ""),
        "summary": content.summary or "",
        "vendor": str(extra.get("vendor") or ""),
        "role": c.headline or "",
        "total_experience": f"{float(years):g}" if years is not None else "",
    }


def _fill_paragraph(paragraph, values: dict[str, str]) -> None:
    """Replace scalar tokens while preserving the first run's formatting.

    Word splits placeholders across runs, so the whole paragraph is rebuilt:
    the substituted text goes into run 0 (which carries the intended styling)
    and the remaining runs are blanked rather than removed, so numbering,
    bookmarks and revision marks anchored to them stay valid.
    """
    original = paragraph.text
    if "{{" not in original:
        return
    replaced = _TOKEN.sub(lambda m: values.get(m.group(1).lower(), m.group(0)), original)
    if replaced == original:
        return
    if paragraph.runs:
        paragraph.runs[0].text = replaced
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(replaced)


# ───────────────────────────────────────────────────────────── blocks
def _block_lines(token: str, content: ResumeContent) -> list[tuple[str, bool]]:
    """(text, is_bold) pairs for one block placeholder."""
    out: list[tuple[str, bool]] = []

    if token == "skills":
        for g in content.skill_groups:
            out.append((f"{g.label}: {', '.join(g.items)}", False))

    elif token == "experience":
        for e in content.experience:
            span = " – ".join(x for x in (e.start_label, e.end_label) if x)
            head = " · ".join(x for x in (e.company, e.designation) if x)
            out.append((f"{head}{f'  ({span})' if span else ''}", True))
            if e.description:
                out.append((e.description, False))
            for h in e.highlights or []:
                out.append((f"• {h}", False))

    elif token == "projects":
        for p in content.projects:
            out.append((p.name, True))
            if p.description:
                out.append((p.description, False))
            for r in p.responsibilities or []:
                out.append((f"• {r}", False))
            for a in p.achievements or []:
                out.append((f"• {a}", False))
            if p.technologies:
                out.append((f"Technologies: {', '.join(p.technologies)}", False))

    elif token == "education":
        for ed in content.education:
            bits = [ed.qualification, getattr(ed, "institution", None), getattr(ed, "year", None)]
            out.append((", ".join(str(b) for b in bits if b), False))

    elif token == "certifications":
        for cert in content.certifications or []:
            out.append((str(cert), False))

    return out


def _expand_block(paragraph, token: str, content: ResumeContent) -> None:
    """Replace a block paragraph with one paragraph per generated line."""
    for text, bold in _block_lines(token, content):
        new = paragraph.insert_paragraph_before("")
        new.add_run(text).bold = bold
        try:
            new.style = paragraph.style
        except Exception:  # noqa: BLE001 — a missing style must not abort the render
            pass
    # Drop the placeholder paragraph itself.
    el = paragraph._element
    el.getparent().remove(el)


# ───────────────────────────────────────────────────────────── entrypoint
def render_from_docx_template(
    template_path: str | Path,
    content: ResumeContent,
    output_path: str | Path,
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Fill an uploaded .docx template and save it to ``output_path``."""
    doc = Document(str(template_path))
    values = _scalar_values(content, extra or {})

    def walk(paragraphs) -> None:
        # Snapshot first: _expand_block mutates the paragraph list underneath us.
        for paragraph in list(paragraphs):
            block = _BLOCK.search(paragraph.text or "")
            if block and block.group(1).lower() in BLOCK_TOKENS:
                _expand_block(paragraph, block.group(1).lower(), content)
            else:
                _fill_paragraph(paragraph, values)

    walk(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                walk(cell.paragraphs)
    for section in doc.sections:
        for part in (section.header, section.footer):
            walk(part.paragraphs)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out
