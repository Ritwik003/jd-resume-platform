"""Section 26 — the template layout definition.

A template is data, not code: a JSON document describing which sections appear,
in what order, and with what typography. The HTML preview, the DOCX writer and
the PDF writer all consume this same object, which is what stops the studio
preview from drifting away from the downloaded file.

Adding a template is an INSERT into ``resume_templates`` — no schema change and
no change to the employee/project database, exactly as section 26 requires.
"""

from __future__ import annotations

from typing import Any

from app.config import RESUME_MAX_PAGES, RESUME_MAX_PROJECTS

SECTION_KEYS = ("contact", "summary", "skills", "experience", "projects", "education", "certifications")

DEFAULT_LAYOUT: dict[str, Any] = {
    # One page, always: every resume here is composed for a single JD, and the
    # composition is capped to what one page holds (see ground_rules.fit_one_page).
    "page": {"margin_cm": 1.6, "max_pages": RESUME_MAX_PAGES},
    "style": {
        "font_family": "Calibri",
        "heading_font_family": "Calibri",
        "base_font_size": 10,
        "name_font_size": 20,
        "section_font_size": 12,
        "accent_color": "1F4E79",
        "text_color": "222222",
        "muted_color": "666666",
        "section_rule": True,
        "uppercase_headings": True,
    },
    "sections": [
        {"key": "contact", "visible": True},
        {"key": "summary", "visible": True, "title": "Professional Summary"},
        {"key": "skills", "visible": True, "title": "Technical Skills", "layout": "grouped"},
        {"key": "experience", "visible": True, "title": "Professional Experience"},
        {
            "key": "projects",
            "visible": True,
            "title": "Key Projects",
            "max_items": RESUME_MAX_PROJECTS,
            "show_technologies": True,
        },
        {"key": "education", "visible": True, "title": "Education"},
        {"key": "certifications", "visible": True, "title": "Certifications"},
    ],
}


def merge_layout(layout: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay a stored layout definition onto the defaults."""
    merged: dict[str, Any] = {
        "page": dict(DEFAULT_LAYOUT["page"]),
        "style": dict(DEFAULT_LAYOUT["style"]),
        "sections": [dict(s) for s in DEFAULT_LAYOUT["sections"]],
    }
    if not layout:
        return merged

    merged["page"].update(layout.get("page") or {})
    merged["style"].update(layout.get("style") or {})

    sections = layout.get("sections")
    if isinstance(sections, list) and sections:
        defaults = {s["key"]: s for s in DEFAULT_LAYOUT["sections"]}
        resolved = []
        for section in sections:
            if not isinstance(section, dict) or section.get("key") not in SECTION_KEYS:
                continue
            base = dict(defaults.get(section["key"], {}))
            base.update(section)
            resolved.append(base)
        if resolved:
            merged["sections"] = resolved
    return merged


def visible_sections(layout: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in layout.get("sections", []) if s.get("visible", True)]


#: Templates seeded by ``scripts/seed.py`` (section 26's example list).
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "GENAI_01",
        "name": "GenAI Engineer Template 01",
        "description": "Summary-led layout with projects above experience; suits GenAI and AI roles.",
        "profile_type": "GenAI Engineer",
        "layout": {
            "style": {"accent_color": "2E5E4E", "name_font_size": 22},
            "sections": [
                {"key": "contact", "visible": True},
                {"key": "summary", "visible": True, "title": "Profile"},
                {"key": "skills", "visible": True, "title": "Technical Skills"},
                {
                    "key": "projects",
                    "visible": True,
                    "title": "Key Projects",
                    "max_items": RESUME_MAX_PROJECTS,
                },
                {"key": "experience", "visible": True, "title": "Experience"},
                {"key": "education", "visible": True, "title": "Education"},
                {"key": "certifications", "visible": True, "title": "Certifications"},
            ],
        },
    },
    {
        "code": "AI_01",
        "name": "AI Engineer Template 01",
        "description": "Classic reverse-chronological layout with experience first.",
        "profile_type": "AI Engineer",
        "layout": {"style": {"accent_color": "1F4E79"}},
    },
    {
        "code": "AI_02",
        "name": "AI Engineer Template 02",
        "description": "Compact variant with a condensed skills grid and tighter margins.",
        "profile_type": "AI Engineer",
        "layout": {
            "page": {"max_pages": RESUME_MAX_PAGES, "margin_cm": 1.2},
            "style": {"base_font_size": 9, "name_font_size": 18, "section_rule": False},
            "sections": [
                {"key": "contact", "visible": True},
                {"key": "summary", "visible": True, "title": "Summary"},
                {"key": "skills", "visible": True, "title": "Skills", "layout": "inline"},
                {"key": "projects", "visible": True, "title": "Projects", "max_items": RESUME_MAX_PROJECTS},
                {"key": "experience", "visible": True, "title": "Experience"},
                {"key": "education", "visible": True, "title": "Education"},
                {"key": "certifications", "visible": False},
            ],
        },
    },
    {
        "code": "BACKEND_01",
        "name": "Backend Engineer Template 01",
        "description": "Experience-led layout emphasising systems and scale.",
        "profile_type": "Backend Engineer",
        "layout": {"style": {"accent_color": "44403C"}},
    },
    {
        "code": "FRONTEND_01",
        "name": "Frontend Engineer Template 01",
        "description": "Projects-forward layout for UI-heavy roles.",
        "profile_type": "Frontend Engineer",
        "layout": {"style": {"accent_color": "7A3E9D"}},
    },
    {
        "code": "FULLSTACK_01",
        "name": "Full Stack Engineer Template 01",
        "description": "Balanced layout covering both frontend and backend delivery.",
        "profile_type": "Full Stack Engineer",
        "layout": {"style": {"accent_color": "1D6F73"}},
    },
]
