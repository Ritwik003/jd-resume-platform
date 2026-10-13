"""DOCX generation from ResumeContent + layout definition."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.schemas.resume_content import ResumeContent
from app.services.rendering.layout import merge_layout, visible_sections


def _rgb(hex_color: str) -> RGBColor:
    value = (hex_color or "222222").lstrip("#")
    return RGBColor(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _bottom_border(paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "D9D9D9")
    borders.append(bottom)
    p_pr.append(borders)


def _add_heading(doc: Document, title: str, style: dict[str, Any]) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(8)
    paragraph.paragraph_format.space_after = Pt(3)
    run = paragraph.add_run(title.upper() if style.get("uppercase_headings") else title)
    run.bold = True
    run.font.size = Pt(style["section_font_size"])
    run.font.color.rgb = _rgb(style["accent_color"])
    run.font.name = style.get("heading_font_family", style["font_family"])
    if style.get("section_rule"):
        _bottom_border(paragraph)


def _add_body(doc: Document, text: str, style: dict[str, Any], *, italic: bool = False, muted: bool = False,
              size_delta: float = 0, bold: bool = False, align=None, space_after: int = 2):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(space_after)
    if align is not None:
        paragraph.alignment = align
    run = paragraph.add_run(text)
    run.italic = italic
    run.bold = bold
    run.font.size = Pt(style["base_font_size"] + size_delta)
    run.font.name = style["font_family"]
    run.font.color.rgb = _rgb(style["muted_color"] if muted else style["text_color"])
    return paragraph


def _add_bullets(doc: Document, items: list[str], style: dict[str, Any]) -> None:
    for item in items:
        if not item or not item.strip():
            continue
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(1)
        paragraph.paragraph_format.left_indent = Cm(0.6)
        run = paragraph.add_run(item.strip())
        run.font.size = Pt(style["base_font_size"])
        run.font.name = style["font_family"]
        run.font.color.rgb = _rgb(style["text_color"])


def _add_two_column_row(doc: Document, left: str, right: str, style: dict[str, Any], *, bold_left: bool = True):
    """A title on the left and dates on the right, using a tab stop."""
    from docx.enum.text import WD_TAB_ALIGNMENT

    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.tab_stops.add_tab_stop(Cm(17.0), WD_TAB_ALIGNMENT.RIGHT)

    run = paragraph.add_run(left)
    run.bold = bold_left
    run.font.size = Pt(style["base_font_size"])
    run.font.name = style["font_family"]
    run.font.color.rgb = _rgb(style["text_color"])

    if right:
        run = paragraph.add_run("\t" + right)
        run.font.size = Pt(style["base_font_size"])
        run.font.name = style["font_family"]
        run.font.color.rgb = _rgb(style["muted_color"])
    return paragraph


def _slug(text: str) -> str:
    """Match a custom block to its window row the way content_window does."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def render_docx(content: ResumeContent, layout_definition: dict[str, Any] | None, output_path: str | Path) -> Path:
    layout = merge_layout(layout_definition)
    style = layout["style"]
    page = layout["page"]

    doc = Document()
    for section in doc.sections:
        margin = Cm(float(page.get("margin_cm", 1.6)))
        section.top_margin = section.bottom_margin = margin
        section.left_margin = section.right_margin = margin

    normal = doc.styles["Normal"]
    normal.font.name = style["font_family"]
    normal.font.size = Pt(style["base_font_size"])

    # The content window, when this resume has one, decides which sections
    # print and in what order — the same list the PDF is filled from. A Word
    # download that still carried a section the operator removed would be the
    # one artefact disagreeing with the rest.
    window = [spec for spec in content.sections if spec.enabled]
    window_order = {spec.kind: i for i, spec in enumerate(window)}
    window_titles = {spec.kind: spec.title for spec in window if spec.kind != "custom"}
    windowed = bool(content.sections)

    sections = list(visible_sections(layout))
    if windowed:
        sections = [s for s in sections if s["key"] == "contact" or s["key"] in window_order]
        sections.sort(key=lambda s: -1 if s["key"] == "contact" else window_order[s["key"]])

    # Does this design print a Projects section of its own? Several shipped
    # templates (the ESSENTIALS and NESTED shapes in
    # app/resume_templates/library) deliberately do NOT -- they print each
    # project under the employer it was built for, straight out of the
    # experience branch. Their LaTeX does that with ``job.projects``; without
    # the same rule here the Word download of one of those designs printed the
    # employers as bare headings and dropped every project on the resume.
    _has_projects_section = any(s["key"] == "projects" for s in sections)

    def _projects_under(exp) -> list:
        """The projects paired with this employer, in resume order.

        Pairing is decided once at composition and carried on the project
        (see resume_composer._pair_projects_to_companies), so match on the
        company id first and fall back to the name for a project an operator
        added by hand in the studio.
        """
        out = []
        for project in content.projects:
            if exp.source_company_id and project.source_company_id:
                if str(project.source_company_id) == str(exp.source_company_id):
                    out.append(project)
            elif project.company and exp.company and project.company == exp.company:
                out.append(project)
        return out

    def _write_project(project, *, nested: bool) -> None:
        _add_two_column_row(doc, f"Project: {project.name}" if nested else project.name, "", style)
        if project.description:
            _add_body(doc, project.description, style, space_after=1)
        _add_bullets(doc, project.responsibilities, style)
        _add_bullets(doc, project.achievements, style)
        if project.technologies:
            _add_body(doc, f"Technologies: {', '.join(project.technologies)}", style,
                      muted=True, size_delta=-0.5, space_after=4)

    for section in sections:
        key = section["key"]
        title = window_titles.get(key) or section.get("title", key.title())

        if key == "contact":
            c = content.contact
            name_p = doc.add_paragraph()
            name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            name_p.paragraph_format.space_after = Pt(0)
            run = name_p.add_run(c.full_name)
            run.bold = True
            run.font.size = Pt(style["name_font_size"])
            run.font.color.rgb = _rgb(style["accent_color"])
            run.font.name = style.get("heading_font_family", style["font_family"])

            if c.headline:
                _add_body(doc, c.headline, style, muted=True, size_delta=1,
                          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
            bits = [b for b in (c.email, c.phone, c.location, *c.links) if b]
            if bits:
                _add_body(doc, "  |  ".join(bits), style, muted=True, size_delta=-0.5,
                          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=4)

        elif key == "summary" and content.summary:
            _add_heading(doc, title, style)
            _add_body(doc, content.summary, style, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=3)

        elif key == "skills" and content.skill_groups:
            _add_heading(doc, title, style)
            if section.get("layout") == "inline":
                _add_body(doc, ", ".join(i for g in content.skill_groups for i in g.items), style)
            else:
                table = doc.add_table(rows=0, cols=2)
                table.autofit = True
                for group in content.skill_groups:
                    cells = table.add_row().cells
                    run = cells[0].paragraphs[0].add_run(group.label)
                    run.bold = True
                    run.font.size = Pt(style["base_font_size"])
                    run.font.name = style["font_family"]
                    run = cells[1].paragraphs[0].add_run(", ".join(group.items))
                    run.font.size = Pt(style["base_font_size"])
                    run.font.name = style["font_family"]

        elif key == "experience" and content.experience:
            _add_heading(doc, title, style)
            for exp in content.experience:
                dates = " – ".join(b for b in (exp.start_label, exp.end_label) if b)
                _add_two_column_row(doc, exp.company, dates, style)
                if exp.designation:
                    _add_body(doc, exp.designation, style, italic=True, muted=True, space_after=1)
                # What the engagement was, before the remit it carried. Without
                # it an employer is a name and a date range and nothing else.
                if exp.description:
                    _add_body(doc, exp.description, style, space_after=1)
                _add_bullets(doc, exp.highlights, style)
                # Only when the design has no Projects section of its own, or
                # the work would print nowhere at all.
                if not _has_projects_section:
                    for project in _projects_under(exp):
                        _write_project(project, nested=True)

        elif key == "projects" and content.projects:
            # Grouped under the employer each project was built at, matching
            # the templates and the HTML preview. A flat list naming the
            # company on every row says the same thing five times and leaves
            # the reader to assemble the grouping themselves.
            _add_heading(doc, title, style)
            for exp, owned in content.projects_by_company():
                if not owned:
                    continue
                if exp is not None:
                    dates = " – ".join(b for b in (exp.start_label, exp.end_label) if b)
                    _add_two_column_row(doc, exp.company, dates, style)
                for project in owned:
                    _write_project(project, nested=exp is not None)

        elif key == "education" and content.education:
            _add_heading(doc, title, style)
            for edu in content.education:
                _add_body(doc, " · ".join(b for b in (edu.qualification, edu.institution, edu.year) if b), style)

        elif key == "certifications" and content.certifications:
            _add_heading(doc, title, style)
            _add_bullets(doc, content.certifications, style)

    # Sections the operator added in the studio. The .tex prints these through
    # the design's own section macro; here they are ordinary headings, printed
    # after the standard blocks because a Word file has no design to place
    # them in.
    kept = {_slug(spec.title) for spec in window if spec.kind == "custom"} if windowed else None
    for custom in content.custom_sections:
        if kept is not None and _slug(custom.title) not in kept:
            continue
        if not (custom.body.strip() or custom.items):
            continue
        _add_heading(doc, custom.title, style)
        if custom.body.strip():
            _add_body(doc, custom.body.strip(), style)
        if custom.items:
            _add_bullets(doc, custom.items, style)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
