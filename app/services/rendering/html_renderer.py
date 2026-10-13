"""HTML preview for the studio's left pane (section 21).

Uses the same layout definition as the DOCX and PDF writers so the preview shows
what will actually be downloaded.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from app.schemas.resume_content import ResumeContent
from app.services import presentation
from app.services.rendering.layout import merge_layout, visible_sections


#: Shown where an enabled section has no content yet. The Content view is an
#: EDITING surface, not the deliverable, and a section the operator has turned
#: on has to be visible there or there is nothing to aim at: adding
#: Certifications and then seeing no change reads as the button being broken,
#: and asking the assistant to fill in a section you cannot see is asking it to
#: work on something invisible.
#:
#: Only ever rendered here. The PDF and the DOCX still skip an empty section,
#: because an "EDUCATION" heading with nothing under it on a resume that goes
#: to a vendor is a defect, not a placeholder.
def _empty_slot(title: str, style: dict) -> str:
    return (
        _heading(title, style)
        + f'<p style="margin:0;color:#{style["muted_color"]};font-style:italic;">'
        + "Nothing here yet \u2014 add it from the AI Assistant, and it will print "
        + "in the design too.</p>"
    )


def _heading(title: str, style: dict[str, Any]) -> str:
    text = escape(title.upper() if style.get("uppercase_headings") else title)
    rule = "border-bottom:1px solid #d9d9d9;" if style.get("section_rule") else ""
    return (
        f'<h2 style="font-size:{style["section_font_size"]}pt;color:#{style["accent_color"]};'
        f'margin:14px 0 6px;padding-bottom:3px;letter-spacing:.06em;{rule}">{text}</h2>'
    )


def _project_html(
    project: Any, style: dict[str, Any], section: dict[str, Any], *, nested: bool = False
) -> str:
    """One project block, used both nested under an employer and standalone."""
    indent = "margin:6px 0 6px 12px;" if nested else "margin-bottom:9px;"
    label = f"Project: {project.name}" if nested else project.name
    out = [f'<div style="{indent}">', f'<div style="font-weight:600;">{escape(label)}</div>']
    if project.description:
        out.append(f'<div style="margin-top:2px;">{escape(project.description)}</div>')
    if project.responsibilities:
        out.append('<ul style="margin:4px 0 0 18px;padding:0;">')
        out.extend(f"<li>{escape(r)}</li>" for r in project.responsibilities)
        out.append("</ul>")
    if project.achievements:
        out.append('<ul style="margin:2px 0 0 18px;padding:0;">')
        out.extend(f"<li>{escape(a)}</li>" for a in project.achievements)
        out.append("</ul>")
    if section.get("show_technologies", True) and project.technologies:
        out.append(
            f'<div style="margin-top:3px;font-size:{style["base_font_size"] - 0.5}pt;'
            f'color:#{style["muted_color"]};"><em>Technologies:</em> '
            f'{escape(", ".join(project.technologies))}</div>'
        )
    out.append("</div>")
    return "".join(out)


def _slug(text: str) -> str:
    """Match a custom block to its window row the way content_window does."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def render_html(content: ResumeContent, layout_definition: dict[str, Any] | None = None) -> str:
    layout = merge_layout(layout_definition)
    style = dict(layout["style"])
    parts: list[str] = []

    # The preview has to show what the PDF will show, so it reads the same
    # presentation settings the LaTeX override block is built from. Without
    # this the operator recolours the resume, sees nothing change, and asks
    # again — see app/services/presentation.py.
    look = presentation.get(content)
    accent = presentation.resolve_color(look.get("accent_color") or "")
    if accent:
        style["accent_color"] = accent
    align = {"left": "left", "right": "right"}.get(look.get("header_align") or "", "center")

    ordered = presentation.ordered_sections(content)

    # The content window, when this resume has one. It is the chosen
    # template's own section list, and it outranks the layout definition's
    # defaults: a section hidden there must be hidden HERE too, or the studio
    # shows the operator a section their PDF will not have. The window's kinds
    # are the layout's keys, which is what lets the two line up.
    window = [s for s in content.sections if s.enabled]
    window_order = {spec.kind: i for i, spec in enumerate(window)}
    window_titles = {spec.kind: spec.title for spec in window if spec.kind != "custom"}
    windowed = bool(content.sections)

    # Does this resume print a Key Projects section of its own? The LaTeX
    # templates ask the same question of the same section list
    # (``template_blueprint.build_context``), and the preview has to answer it
    # identically or the studio shows one resume and the download is another.
    #
    # This is the bug it fixes: the preview used to nest projects inside the
    # experience section and give the Projects section only the *unpaired*
    # ones. Hiding Professional Experience therefore took every paired project
    # off the page with it — the operator hid one section and the resume went
    # blank — while the PDF, reading the same window, still printed them all.
    projects_visible = (
        any(spec.kind == "projects" for spec in window) if windowed else True
    )

    def section_rank(section: dict[str, Any]) -> int:
        key = section.get("key")
        # Contact is the header; it never moves out of first place.
        if key == "contact":
            return -1
        if windowed:
            return window_order.get(key, len(window_order))
        return ordered.index(key) if key in ordered else len(ordered)

    def in_window(section: dict[str, Any]) -> bool:
        key = section.get("key")
        return not windowed or key == "contact" or key in window_order

    parts.append(
        f'<div class="resume" style="font-family:{escape(style["font_family"])},sans-serif;'
        f'font-size:{style["base_font_size"]}pt;color:#{style["text_color"]};line-height:1.45;'
        f'max-width:820px;margin:0 auto;padding:{style.get("base_font_size", 10) * 2}px;background:#fff;">'
    )

    for section in sorted(
        [s for s in visible_sections(layout) if in_window(s)], key=section_rank
    ):
        key = section["key"]
        # The design's own wording wins over the layout's default: a template
        # that calls its summary "Profile" says Profile in the preview too.
        title = window_titles.get(key) or section.get("title", key.title())
        # Where this section's output starts, so an enabled section that
        # renders nothing can be spotted below rather than silently vanishing.
        mark = len(parts)

        if key == "contact":
            c = content.contact
            bits = [b for b in (c.email, c.phone, c.location, *c.links) if b]
            parts.append(
                f'<div style="text-align:{align};margin-bottom:6px;">'
                f'<div style="font-size:{style["name_font_size"]}pt;font-weight:700;'
                f'color:#{style["accent_color"]};">{escape(c.full_name)}</div>'
                f'<div style="font-size:{style["base_font_size"] + 1}pt;color:#{style["muted_color"]};'
                f'margin-top:2px;">{escape(c.headline)}</div>'
                f'<div style="font-size:{style["base_font_size"] - 0.5}pt;color:#{style["muted_color"]};'
                f'margin-top:4px;">{escape(" | ".join(bits))}</div></div>'
            )

        elif key == "summary" and content.summary:
            parts.append(_heading(title, style))
            parts.append(f'<p style="margin:0;text-align:justify;">{escape(content.summary)}</p>')

        elif key == "skills" and content.skill_groups:
            parts.append(_heading(title, style))
            if section.get("layout") == "inline":
                everything = ", ".join(item for g in content.skill_groups for item in g.items)
                parts.append(f'<p style="margin:0;">{escape(everything)}</p>')
            else:
                parts.append('<table style="width:100%;border-collapse:collapse;">')
                for group in content.skill_groups:
                    parts.append(
                        f'<tr><td style="width:150px;vertical-align:top;padding:2px 8px 2px 0;font-weight:600;">'
                        f'{escape(group.label)}</td>'
                        f'<td style="padding:2px 0;">{escape(", ".join(group.items))}</td></tr>'
                    )
                parts.append("</table>")

        elif key == "experience" and content.experience:
            parts.append(_heading(title, style))
            for exp, owned in content.projects_by_company():
                if exp is None:
                    continue
                dates = " – ".join(b for b in (exp.start_label, exp.end_label) if b)
                parts.append(
                    f'<div style="margin-bottom:8px;">'
                    f'<div style="display:flex;justify-content:space-between;">'
                    f'<span style="font-weight:600;">{escape(exp.company)}</span>'
                    f'<span style="color:#{style["muted_color"]};">{escape(dates)}</span></div>'
                    f'<div style="color:#{style["muted_color"]};font-style:italic;">'
                    f'{escape(exp.designation or "")}</div>'
                )
                # What the engagement was. An employer line carrying only a
                # name, a title and a date range is what this section used to
                # print four times in a row.
                if exp.description:
                    parts.append(f'<div style="margin-top:2px;">{escape(exp.description)}</div>')
                if exp.highlights:
                    parts.append('<ul style="margin:4px 0 0 18px;padding:0;">')
                    parts.extend(f"<li>{escape(h)}</li>" for h in exp.highlights)
                    parts.append("</ul>")
                # Nested only when no Projects section is printing them, the
                # same condition the templates use.
                if not projects_visible:
                    for project in owned:
                        parts.append(_project_html(project, style, section, nested=True))
                parts.append("</div>")

        elif key == "projects" and content.projects:
            # Grouped under the employer the work was done at, in the employer
            # order the experience section prints — not a flat list, and not
            # only the unpaired ones.
            parts.append(_heading(title, style))
            for exp, owned in content.projects_by_company():
                if not owned:
                    continue
                if exp is not None:
                    dates = " – ".join(b for b in (exp.start_label, exp.end_label) if b)
                    parts.append(
                        f'<div style="display:flex;justify-content:space-between;margin-top:6px;">'
                        f'<span style="font-weight:600;">{escape(exp.company)}</span>'
                        f'<span style="color:#{style["muted_color"]};">{escape(dates)}</span></div>'
                    )
                for project in owned:
                    parts.append(_project_html(project, style, section, nested=exp is not None))

        elif key == "education" and content.education:
            parts.append(_heading(title, style))
            for edu in content.education:
                line = " · ".join(b for b in (edu.qualification, edu.institution, edu.year) if b)
                parts.append(f'<div>{escape(line)}</div>')

        elif key == "certifications" and content.certifications:
            parts.append(_heading(title, style))
            parts.append('<ul style="margin:0 0 0 18px;padding:0;">')
            parts.extend(f"<li>{escape(c)}</li>" for c in content.certifications)
            parts.append("</ul>")

        # Nothing matched, or what matched had no data. Either way the operator
        # switched this section on and it has to show.
        if len(parts) == mark and key != "contact":
            parts.append(_empty_slot(title, style))

    # Sections the operator added in the studio. They sit after the standard
    # ones because the layout definition has no slot to place them in — a
    # named position would mean editing the template to add a section.
    if windowed:
        # ...unless the window says otherwise, in which case only the ones it
        # still carries are shown, in the order it carries them.
        kept = {_slug(spec.title) for spec in window if spec.kind == "custom"}
        customs = [c for c in content.custom_sections if _slug(c.title) in kept]
    else:
        customs = list(content.custom_sections)

    for custom in customs:
        if not (custom.body.strip() or custom.items):
            # The whole point of adding one is to fill it next; skipping it
            # here meant "+ Add a section" looked like it had done nothing.
            parts.append(_empty_slot(custom.title, style))
            continue
        parts.append(_heading(custom.title, style))
        if custom.body.strip():
            parts.append(f'<p style="margin:0;">{escape(custom.body)}</p>')
        if custom.items:
            parts.append('<ul style="margin:4px 0 0 18px;padding:0;">')
            parts.extend(f"<li>{escape(i)}</li>" for i in custom.items)
            parts.append("</ul>")

    parts.append("</div>")
    return "".join(parts)
