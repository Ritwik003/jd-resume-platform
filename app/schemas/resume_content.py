"""The structured resume document (``ResumeVersion.content_json``).

This is the single source of truth for a resume. The HTML preview, the DOCX and
the PDF are all projections of this object through a template's layout
definition, so the three renderings cannot drift apart.

Storing structure (rather than free text) is what makes the agentic edits in
section 22 deterministic: "move AWS higher in the skills" becomes a list
reorder, not a full-document regeneration.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CONTENT_SCHEMA_VERSION = "1.0"


class ContactBlock(BaseModel):
    full_name: str = ""
    headline: str = ""
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class SkillGroup(BaseModel):
    """A labelled row in the skills section, e.g. "Cloud: AWS, Docker"."""

    label: str = ""
    tier: Literal["PRIMARY", "SECONDARY"] = "PRIMARY"
    items: list[str] = Field(default_factory=list)


class ProjectBlock(BaseModel):
    #: Links back to the master project so edits stay traceable to their source.
    source_project_id: str | None = None
    name: str = ""
    role: str | None = None
    description: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)

    #: Which employer this project is shown under on THIS resume. A project in
    #: the bank carries no company of its own (see app/models/project.py) — the
    #: two are independent pools — so the pairing is a composition decision,
    #: made once at auto-selection and then held steady. It is what lets a
    #: template print "Apisero → Project: Puno.AI" the way a real resume does,
    #: and it is what dates a project: a project inherits the period of the
    #: employment it sits under, which is the only clock it has.
    company: str | None = None
    source_company_id: str | None = None


class ExperienceBlock(BaseModel):
    source_company_id: str | None = None
    company: str = ""
    designation: str | None = None
    start_label: str | None = None
    end_label: str | None = None
    location: str | None = None

    #: One or two sentences saying what the engagement at this employer WAS,
    #: written for the JD in front of it. An employer line carrying only a name
    #: and a date range tells a reader nothing — it is the shape a resume has
    #: when nobody filled it in — and four of them in a row is what the
    #: Professional Experience section used to print. The company is a fact and
    #: never changes; what this says about the work there is composed for the
    #: role, the same way a project description is.
    description: str = ""

    #: Responsibilities held at this employer, above the level of any one
    #: project. Distinct from a project's bullets on purpose: these are the
    #: standing remit ("owned the payments service and its on-call"), the
    #: projects are the things built inside it.
    highlights: list[str] = Field(default_factory=list)


class EducationBlock(BaseModel):
    qualification: str = ""
    institution: str | None = None
    year: str | None = None


class CustomSection(BaseModel):
    """A section the operator added in the studio that the schema has no field for.

    The named sections above cover what a resume normally carries, but not
    what a particular JD occasionally asks for — Publications, Patents,
    Speaking, Open Source, Security Clearance. Rather than grow the schema
    every time one is wanted, an arbitrary titled block can be added to a
    single composition. It belongs to that resume only; nothing about the
    person's stored record changes.
    """

    title: str = ""
    #: Free prose shown under the heading, before any bullets.
    body: str = ""
    items: list[str] = Field(default_factory=list)


class SectionSpec(BaseModel):
    """One row of the content window — one section of the chosen design.

    Seeded from the template's blueprint when a session picks a template, so
    the window shows the design's OWN sections in the design's own order and
    wording: a template whose summary is called "Profile" offers Profile, and
    one with no Education section never offers Education. The operator then
    edits this list — disable a section, rename it, add one the design does not
    have, reorder — and the same list drives the render, which is what keeps
    the window and the printed resume from being two different opinions.
    """

    #: Stable identity for the row; slug of the title it was created with.
    key: str = ""
    #: The heading as it will print. Renaming a section changes this alone.
    title: str = ""
    #: Which content feeds it — summary/skills/experience/projects/education/
    #: certifications, or "custom" for a titled block with its own text.
    kind: str = "custom"
    #: A disabled section keeps its place and its content but does not print,
    #: so turning it back on is one click rather than a regeneration.
    enabled: bool = True
    #: How the design opens a section, captured at upload. A section the
    #: operator ADDS is printed through this, so it looks native rather than
    #: bolted on.
    macro: str | None = None
    #: True when the design has no block of its own for this section.
    added: bool = False


class ResumeContent(BaseModel):
    """Complete structured resume."""

    schema_version: str = CONTENT_SCHEMA_VERSION
    contact: ContactBlock = Field(default_factory=ContactBlock)
    summary: str = ""
    skill_groups: list[SkillGroup] = Field(default_factory=list)
    experience: list[ExperienceBlock] = Field(default_factory=list)
    projects: list[ProjectBlock] = Field(default_factory=list)
    education: list[EducationBlock] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    #: Operator-added sections the fixed fields above do not cover. Printed
    #: after the standard ones by every renderer.
    custom_sections: list[CustomSection] = Field(default_factory=list)
    #: The content window: which sections this resume prints, in which order,
    #: under which headings. Seeded from the chosen template's blueprint and
    #: edited from the studio or the chat. Empty means "not seeded yet" — a
    #: built-in layout template, or a version composed before the blueprint
    #: pipeline — and every renderer falls back to its own default order.
    sections: list[SectionSpec] = Field(default_factory=list)
    #: Rendering hints a template may honour (e.g. {"max_pages": 2}).
    render_hints: dict[str, Any] = Field(default_factory=dict)

    # ---------------------------------------------------------------- helpers
    def all_technologies(self) -> list[str]:
        names: list[str] = []
        for group in self.skill_groups:
            names.extend(group.items)
        for project in self.projects:
            names.extend(project.technologies)
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(name.strip())
        return unique

    def projects_by_company(self) -> list[tuple[ExperienceBlock | None, list[ProjectBlock]]]:
        """Group projects under the employment each was composed against.

        Returns employments in their existing (most-recent-first) order, each
        with the projects assigned to it, followed by one final entry keyed on
        ``None`` for projects that were never paired — a project composed in
        the studio, or a resume generated before pairing existed. Templates
        that print projects nested under employers walk this; templates with a
        standalone Projects section ignore it and read ``projects`` directly.
        """
        by_company: dict[str, list[ProjectBlock]] = {}
        orphans: list[ProjectBlock] = []
        for project in self.projects:
            key = project.source_company_id or ""
            if key and any(e.source_company_id == key for e in self.experience):
                by_company.setdefault(key, []).append(project)
            else:
                orphans.append(project)

        grouped: list[tuple[ExperienceBlock | None, list[ProjectBlock]]] = [
            (exp, by_company.get(exp.source_company_id or "", [])) for exp in self.experience
        ]
        if orphans:
            grouped.append((None, orphans))
        return grouped

    def text_blocks(self) -> list[tuple[str, str]]:
        """(content_path, text) pairs for the validation engine to scan."""
        blocks: list[tuple[str, str]] = [("summary", self.summary)]
        for i, project in enumerate(self.projects):
            blocks.append((f"projects[{i}].description", project.description))
            for j, line in enumerate(project.responsibilities):
                blocks.append((f"projects[{i}].responsibilities[{j}]", line))
            for j, line in enumerate(project.achievements):
                blocks.append((f"projects[{i}].achievements[{j}]", line))
        for i, exp in enumerate(self.experience):
            for j, line in enumerate(exp.highlights):
                blocks.append((f"experience[{i}].highlights[{j}]", line))
        return [(path, text) for path, text in blocks if text and text.strip()]

    def clone(self) -> ResumeContent:
        return ResumeContent.model_validate(self.model_dump(mode="json"))
