"""Section 22 — the edit tools the chatbot may call.

Every studio edit goes through one of these functions. The model chooses *which*
tool and *what arguments*; it never rewrites ``content_json`` wholesale. That is
what makes instructions like "move AWS higher in the skills" reliable instead of
a fresh roll of the dice, and it is what lets each change be described, audited
and reverted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.resume_content import (
    CustomSection,
    EducationBlock,
    ExperienceBlock,
    ProjectBlock,
    ResumeContent,
    SkillGroup,
)
from app.services import content_window, presentation


@dataclass(slots=True)
class ToolOutcome:
    applied: bool
    description: str


@dataclass(slots=True)
class ToolContext:
    """What a tool needs beyond the document itself.

    Most edits are pure rearrangements of ``content``. A few — "add another
    project under Accodian", "add one more employer" — have to go back to the
    bank for something that is not on the resume yet, and those need the
    session they are being composed for.
    """

    db: Any
    session: Any


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", (value or "").lower())


# ----------------------------------------------------------------- summary ops
def apply_next_summary_format(content: ResumeContent, *, context: Any = None, **kwargs: Any) -> ToolOutcome:
    """Try the next standard summary format nobody has used on this resume yet.

    "Change the summary" and "improve the summary" land here FIRST, not on a
    freehand rewrite: six proven shapes (role-led, skills-first, impact-led,
    career-arc, concise, value-proposition — see summary_templates.py) are
    assembled from facts already on the page, so trying another one can never
    introduce a claim the resume does not already make. Once every shape has
    had a turn on this resume, this tool says so and the agent falls back to
    ``rewrite_summary`` for a freehand version instead.
    """
    from app.services import resume_composer, summary_templates

    if context is None or context.db is None or context.session is None:
        return ToolOutcome(False, "The knowledge base is not reachable from here.")

    from app.models.requirement import Requirement

    requirement = (
        context.db.query(Requirement).filter(Requirement.id == context.session.requirement_id).first()
    )
    jd_role = requirement.parsed_role if requirement else None
    target_years = requirement.min_experience_years if requirement else None
    domain = requirement.domain if requirement else None

    tried = content.render_hints.get("summary_templates_tried") or []
    remaining = [i for i in range(len(summary_templates.SUMMARY_FORMATS)) if i not in tried]
    if not remaining:
        return ToolOutcome(
            False,
            "Every standard summary format has already been tried on this resume. "
            "Use rewrite_summary with fresh text instead.",
        )

    inputs = resume_composer.summary_inputs_from(
        content, jd_role=jd_role, target_years=target_years, domain=domain
    )
    format_index = remaining[0]
    resume_composer.apply_summary_format(content, format_index, inputs)
    label = summary_templates.SUMMARY_FORMATS[format_index].label
    left = len(remaining) - 1
    more = f" {left} standard format{'s' if left != 1 else ''} left to try after this." if left else (
        " That was the last standard format — a further change will need rewrite_summary."
    )
    return ToolOutcome(True, f"Applied the '{label}' summary format.{more}")


def rewrite_summary(
    content: ResumeContent,
    *,
    text: str | None = None,
    direction: str | None = None,
    context: Any = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Rewrite the professional summary.

    Three ways in, in order of how specific the operator was:

    * ``text`` — they dictated the wording. Used verbatim; nothing to decide.
    * ``direction`` — they said what they wanted CHANGED ("shorter", "lead
      with the Azure work", "more about the platform, less about the models").
      That goes to ``summary_writer`` along with the whole resume, so the new
      summary is written to the instruction and still checked against the
      grounding rules before it is used.
    * ``max_sentences`` — a pure trim of what is already there, no model call.

    The direction path is the one that matters. Answering "make it longer and
    more specific" by rotating to a different fixed template — which is what
    happened before — changes the words without answering the request.
    """
    if text and text.strip():
        content.summary = text.strip()
        return ToolOutcome(True, "Rewrote the professional summary.")

    asked = (direction or "").strip()
    if asked:
        from app.services import summary_writer

        role, years, jd_skills, missing = _jd_summary_inputs(content, context)
        written, _model = summary_writer.write(
            content,
            role=role,
            years=years,
            jd_skills=jd_skills,
            missing_skills=missing,
            direction=asked,
        )
        if written:
            content.summary = written
            return ToolOutcome(True, f"Rewrote the summary: {asked}")
        # Falling back to a template here would answer a specific request with
        # a generic paragraph, which is worse than saying it did not work.
        return ToolOutcome(
            False,
            "Could not write a summary to that instruction — the model was "
            "unreachable or what it returned did not pass the grounding "
            "checks. Try again, or give the exact wording to use.",
        )

    max_sentences = kwargs.get("max_sentences")
    if max_sentences and content.summary:
        sentences = re.split(r"(?<=[.!?])\s+", content.summary.strip())
        content.summary = " ".join(sentences[: int(max_sentences)])
        return ToolOutcome(True, f"Shortened the summary to {max_sentences} sentences.")

    return ToolOutcome(
        False,
        "Say what the summary should change to — either the wording itself, or "
        "what you want different about it.",
    )


def _jd_summary_inputs(
    content: ResumeContent, context: Any
) -> tuple[str, float | None, list[str], list[str]]:
    """The JD facts the summary writer needs, from the session if it is there.

    Without a session — a tool run in isolation — the resume's own headline
    and an empty skill list still produce a usable summary; what is lost is
    the JD's spelling of the technologies and the missing-skill guard, so the
    forbidden list is empty rather than wrong.
    """
    if context is None or getattr(context, "db", None) is None or getattr(context, "session", None) is None:
        return (content.contact.headline or "", None, [], [])

    from app.models.requirement import Requirement

    requirement = (
        context.db.query(Requirement).filter(Requirement.id == context.session.requirement_id).first()
    )
    if requirement is None:
        return (content.contact.headline or "", None, [], [])

    from app.services import resume_composer

    mandatory, preferred = resume_composer._jd_skill_names(context.db, context.session)
    evidenced = {name.lower() for name in content.all_technologies()}
    return (
        requirement.parsed_role or content.contact.headline or "",
        requirement.min_experience_years,
        sorted(n for n in mandatory if n.lower() in evidenced) + sorted(preferred),
        sorted(n for n in mandatory if n.lower() not in evidenced),
    )


# ------------------------------------------------------------------ skill ops
def _find_skill(content: ResumeContent, technology: str) -> tuple[SkillGroup, int] | None:
    target = _norm(technology)
    for group in content.skill_groups:
        for index, item in enumerate(group.items):
            if _norm(item) == target:
                return group, index
    for group in content.skill_groups:
        for index, item in enumerate(group.items):
            if target and target in _norm(item):
                return group, index
    return None


def reorder_skill(content: ResumeContent, *, technology: str, position: int = 0, **kwargs: Any) -> ToolOutcome:
    """Move a technology within the skills section.

    ``position`` 0 means the very top of the section, which may mean moving the
    item into the first group — "move AWS higher in the skills" should promote it
    past the other groups, not just within its own.
    """
    found = _find_skill(content, technology)
    if not found:
        return ToolOutcome(False, f"'{technology}' is not in the skills section.")

    group, index = found
    item = group.items.pop(index)
    promote_to_first_group = position == 0 and content.skill_groups and group is not content.skill_groups[0]

    if promote_to_first_group:
        content.skill_groups[0].items.insert(0, item)
        content.skill_groups = [g for g in content.skill_groups if g.items]
        return ToolOutcome(True, f"Moved {item} to the top of the skills section.")

    group.items.insert(max(0, min(position, len(group.items))), item)
    return ToolOutcome(True, f"Moved {item} to position {position + 1} in '{group.label}'.")


def remove_skill(content: ResumeContent, *, technology: str, **kwargs: Any) -> ToolOutcome:
    found = _find_skill(content, technology)
    if not found:
        return ToolOutcome(False, f"'{technology}' is not in the skills section.")
    group, index = found
    removed = group.items.pop(index)
    content.skill_groups = [g for g in content.skill_groups if g.items]
    return ToolOutcome(True, f"Removed {removed} from the skills section.")


def add_skill(
    content: ResumeContent, *, technology: str, group_label: str | None = None, **kwargs: Any
) -> ToolOutcome:
    """Add a technology to the skills section.

    Grounding is enforced by the caller, not here: the studio compares the result
    against the person's stored technologies and raises a
    ``TECHNOLOGY_NOT_IN_PROFILE`` warning when the addition is not backed by the
    profile, so the user makes an explicit decision about it.
    """
    if _find_skill(content, technology):
        return ToolOutcome(False, f"'{technology}' is already listed.")
    label = group_label or (content.skill_groups[0].label if content.skill_groups else "Technologies")
    for group in content.skill_groups:
        if group.label.lower() == label.lower():
            group.items.append(technology)
            return ToolOutcome(True, f"Added {technology} to '{group.label}'.")
    content.skill_groups.append(SkillGroup(label=label, tier="PRIMARY", items=[technology]))
    return ToolOutcome(True, f"Added {technology} under a new '{label}' group.")


def reorder_skill_groups(content: ResumeContent, *, order: list[str], **kwargs: Any) -> ToolOutcome:
    wanted = [_norm(label) for label in order]
    ranked = sorted(
        content.skill_groups,
        key=lambda g: wanted.index(_norm(g.label)) if _norm(g.label) in wanted else len(wanted),
    )
    content.skill_groups = ranked
    return ToolOutcome(True, "Reordered the skill groups.")


# ---------------------------------------------------------------- project ops
def _find_project(content: ResumeContent, query: str) -> int | None:
    target = _norm(query)
    if not target:
        return None
    for index, project in enumerate(content.projects):
        if _norm(project.name) == target or str(project.source_project_id) == query:
            return index
    for index, project in enumerate(content.projects):
        haystack = _norm(f"{project.name} {project.description}")
        if target in haystack:
            return index
    m = re.match(r"(?:project\s*)?(\d+)", query.strip().lower())
    if m:
        pos = int(m.group(1)) - 1
        if 0 <= pos < len(content.projects):
            return pos
    return None


def set_primary_project(content: ResumeContent, *, project_query: str, **kwargs: Any) -> ToolOutcome:
    index = _find_project(content, project_query)
    if index is None:
        return ToolOutcome(False, f"No project matching '{project_query}'.")
    project = content.projects.pop(index)
    content.projects.insert(0, project)
    return ToolOutcome(True, f"Made '{project.name}' the first project.")


def reorder_projects(content: ResumeContent, *, order: list[str], **kwargs: Any) -> ToolOutcome:
    resolved: list[ProjectBlock] = []
    remaining = list(content.projects)
    for query in order:
        index = _find_project(ResumeContent(projects=remaining), query)
        if index is not None:
            resolved.append(remaining.pop(index))
    content.projects = resolved + remaining
    return ToolOutcome(True, "Reordered the projects.")


def remove_project(content: ResumeContent, *, project_query: str, **kwargs: Any) -> ToolOutcome:
    index = _find_project(content, project_query)
    if index is None:
        return ToolOutcome(False, f"No project matching '{project_query}'.")
    removed = content.projects.pop(index)
    return ToolOutcome(True, f"Removed '{removed.name}' from the resume.")


def update_project_description(
    content: ResumeContent, *, project_query: str, text: str, **kwargs: Any
) -> ToolOutcome:
    index = _find_project(content, project_query)
    if index is None:
        return ToolOutcome(False, f"No project matching '{project_query}'.")
    content.projects[index].description = text.strip()
    return ToolOutcome(True, f"Rewrote the description for '{content.projects[index].name}'.")


def rewrite_responsibilities(
    content: ResumeContent, *, project_query: str | None = None, items: list[str] | None = None, **kwargs: Any
) -> ToolOutcome:
    if items is None:
        return ToolOutcome(False, "No replacement responsibilities supplied.")
    if project_query:
        index = _find_project(content, project_query)
        if index is None:
            return ToolOutcome(False, f"No project matching '{project_query}'.")
        content.projects[index].responsibilities = [i for i in items if i.strip()]
        return ToolOutcome(True, f"Rewrote responsibilities for '{content.projects[index].name}'.")
    if content.projects:
        content.projects[0].responsibilities = [i for i in items if i.strip()]
        return ToolOutcome(True, f"Rewrote responsibilities for '{content.projects[0].name}'.")
    return ToolOutcome(False, "There are no projects to update.")


# --------------------------------------------------------------- document ops
def set_render_hint(content: ResumeContent, *, key: str, value: Any, **kwargs: Any) -> ToolOutcome:
    content.render_hints[key] = value
    return ToolOutcome(True, f"Set {key} to {value}.")


def update_contact_headline(content: ResumeContent, *, headline: str, **kwargs: Any) -> ToolOutcome:
    content.contact.headline = headline.strip()
    return ToolOutcome(True, "Updated the headline.")


# ------------------------------------------------------------------- registry
def remove_section(content: ResumeContent, *, section: str, **kwargs: Any) -> ToolOutcome:
    """Drop a whole section from this resume.

    When the resume carries a content window — a template whose sections were
    read at upload — this is a window edit: the section stops printing and
    keeps its content, so putting it back is one instruction rather than a
    regeneration. It affects this composition alone; the person's stored
    education and certifications are untouched, which is the section 4
    separation.

    Without a window (a built-in layout template) it falls back to clearing
    the content itself, which is the only way to stop a fixed renderer
    printing it.
    """
    if content.sections:
        return ToolOutcome(*content_window.remove(content, section))

    key = (section or "").strip().lower().rstrip("s")
    if key in ("education", "qualification"):
        if not content.education:
            return ToolOutcome(False, "There is no education section to remove.")
        count = len(content.education)
        content.education = []
        return ToolOutcome(True, f"Removed the education section ({count} entry/entries).")
    if key in ("certification", "certificate", "cert"):
        if not content.certifications:
            return ToolOutcome(False, "There is no certifications section to remove.")
        count = len(content.certifications)
        content.certifications = []
        return ToolOutcome(True, f"Removed the certifications section ({count} item(s)).")
    if key in ("project", "key project"):
        if not content.projects:
            return ToolOutcome(False, "There are no projects to remove.")
        count = len(content.projects)
        content.projects = []
        return ToolOutcome(True, f"Removed all {count} projects.")
    # A section added in the studio is removable by its own title, so whatever
    # add_section created can be taken back off the same way.
    named = (section or "").strip().lower()
    match = next((s for s in content.custom_sections if s.title.lower() == named), None)
    if match is not None:
        content.custom_sections.remove(match)
        return ToolOutcome(True, f"Removed the '{match.title}' section.")

    removable = ["education", "certifications", "projects"]
    removable += [s.title for s in content.custom_sections]
    return ToolOutcome(
        False,
        f"'{section}' is not a removable section. Removable: {', '.join(removable)}.",
    )


def remove_certification(content: ResumeContent, *, certification: str, **kwargs: Any) -> ToolOutcome:
    needle = (certification or "").strip().lower()
    kept = [c for c in content.certifications if needle not in c.lower()]
    if len(kept) == len(content.certifications):
        return ToolOutcome(False, f"No certification matching '{certification}'.")
    removed = len(content.certifications) - len(kept)
    content.certifications = kept
    return ToolOutcome(True, f"Removed {removed} certification(s) matching '{certification}'.")


def rename_project(content: ResumeContent, *, project_query: str, name: str, **kwargs: Any) -> ToolOutcome:
    """Reframe a project's title toward the target role."""
    needle = (project_query or "").strip().lower()
    for project in content.projects:
        if needle in (project.name or "").lower():
            previous = project.name
            project.name = name
            return ToolOutcome(True, f"Renamed '{previous}' to '{name}'.")
    return ToolOutcome(False, f"No project matching '{project_query}'.")



def remove_experience_dates(
    content: ResumeContent, *, company_query: str | None = None, **kwargs: Any
) -> ToolOutcome:
    """The same, for the experience section."""
    targets = content.experience
    if company_query and company_query.strip().lower() not in ("all", "*", "every"):
        index = _find_experience(content, company_query)
        if index is None:
            return ToolOutcome(False, f"No employment matching '{company_query}'.")
        targets = [content.experience[index]]

    if not targets:
        return ToolOutcome(False, "There is no experience section on this resume.")
    for block in targets:
        block.start_label = None
        block.end_label = None
    where = "every employment row" if len(targets) > 1 else f"'{targets[0].company}'"
    return ToolOutcome(True, f"Removed the dates from {where}.")


def set_project_technologies(
    content: ResumeContent, *, project_query: str, technologies: list[str], **kwargs: Any
) -> ToolOutcome:
    index = _find_project(content, project_query)
    if index is None:
        return ToolOutcome(False, f"No project matching '{project_query}'.")
    cleaned = [t.strip() for t in (technologies or []) if str(t).strip()]
    content.projects[index].technologies = cleaned
    return ToolOutcome(
        True, f"Set the technologies on '{content.projects[index].name}' to {', '.join(cleaned) or 'none'}."
    )


def rewrite_achievements(
    content: ResumeContent, *, project_query: str | None = None, items: list[str] | None = None, **kwargs: Any
) -> ToolOutcome:
    if items is None:
        return ToolOutcome(False, "No replacement achievements supplied.")
    index = _find_project(content, project_query) if project_query else (0 if content.projects else None)
    if index is None:
        return ToolOutcome(False, f"No project matching '{project_query}'.")
    content.projects[index].achievements = [i for i in items if str(i).strip()]
    return ToolOutcome(True, f"Rewrote achievements for '{content.projects[index].name}'.")


def add_project(
    content: ResumeContent,
    *,
    name: str,
    company: str | None = None,
    role: str | None = None,
    description: str | None = None,
    responsibilities: list[str] | None = None,
    achievements: list[str] | None = None,
    technologies: list[str] | None = None,
    position: int | None = None,
    context: Any = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Compose a project and put it on this resume.

    Section 17 mix-and-match: the operator may build a project directly. It
    exists on this resume only — nothing is written back to the master pool.

    ``company`` is optional. Left out, this puts the project under the
    resume's CURRENT employer — the first row in ``experience``, kept most-
    recent-first — because that is what "add a project" means without
    qualification: something the person is doing now. An unpaired project
    prints as an orphan and dates nothing, so leaving it homeless is worse
    than a reasonable guess the operator can correct with one more sentence.
    """
    if not (name or "").strip():
        return ToolOutcome(False, "A new project needs a name.")

    # Idempotent by name. The agent sees the conversation history, and a later
    # instruction that mentions an earlier one ("remove the last project and
    # shorten the summary") makes it re-issue the earlier add as well. A resume
    # listing the same project twice is always wrong, so this is the right guard
    # whether or not a model caused it.
    existing = _norm(name)
    for project in content.projects:
        if _norm(project.name) == existing:
            return ToolOutcome(False, f"'{project.name}' is already on this resume.")

    wanted = (company or "").strip()
    matched_company: str | None = None
    matched_source_id: str | None = None
    auto_assigned = False
    if wanted:
        match = next((e for e in content.experience if _norm(e.company) == _norm(wanted)), None)
        matched_company = match.company if match else wanted
        matched_source_id = match.source_company_id if match else None
    elif content.experience:
        current = content.experience[0]
        matched_company, matched_source_id = current.company, current.source_company_id
        auto_assigned = True

    desc = (description or "").strip()
    if desc and context is not None and getattr(context, "db", None) and getattr(context, "session", None):
        moulded = _mould_description_to_jd(
            context, desc, name=name.strip(), technologies=technologies or []
        )
        if moulded:
            desc = moulded

    block = ProjectBlock(
        name=name.strip(),
        # No source_project_id: this block has no row in the bank behind it.
        role=(role or "").strip() or None,
        description=desc,
        responsibilities=[i.strip() for i in (responsibilities or []) if str(i).strip()],
        achievements=[i.strip() for i in (achievements or []) if str(i).strip()],
        technologies=[t.strip() for t in (technologies or []) if str(t).strip()],
        company=matched_company,
        source_company_id=matched_source_id,
    )
    if position is None or position >= len(content.projects):
        content.projects.append(block)
    else:
        content.projects.insert(max(0, int(position)), block)

    where = f" under {matched_company}" if matched_company else ""
    note = " (no employer named, so it was put under the current one)" if auto_assigned else ""
    return ToolOutcome(True, f"Added the project '{block.name}'{where}.{note}")


def _mould_description_to_jd(
    context: Any, description: str, *, name: str, technologies: list[str]
) -> str | None:
    """Angle a hand-written project description at this session's JD.

    Best-effort and silent on failure: a project the operator typed in stays
    exactly as typed if the model is unreachable, rather than blocking the add.
    """
    from app.services.llm.base import LLMPermanentError, LLMTransientError

    try:
        from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS
        from app.models.requirement import Requirement
        from app.services.llm.factory import get_llm_provider
        from app.services.llm.retry import with_retry

        requirement = (
            context.db.query(Requirement).filter(Requirement.id == context.session.requirement_id).first()
        )
        if requirement is None or not (requirement.parsed_role or "").strip():
            return None

        lines = [
            "Rewrite this project description in 1-2 sentences so it reads as work for the",
            f"role '{requirement.parsed_role}'. Lead with the part of the system that role",
            "would own. Keep every fact -- do not invent technologies, numbers or scope beyond",
            f"what is given. Do not rename the project '{name}'.",
            f"Technologies actually used: {', '.join(technologies) or 'not specified'}.",
            "",
            "Original description:",
            description,
            "",
            "Return only the rewritten description, nothing else.",
        ]
        prompt = "\n".join(lines)
        provider = get_llm_provider()
        response = with_retry(
            lambda: provider.chat(
                [{"role": "user", "content": prompt}],
                model=LLM_MODEL,
                temperature=LLM_TEMPERATURE,
                max_tokens=max(LLM_MAX_TOKENS, 4000),
                timeout=LLM_TIMEOUT_SECONDS,
            ),
            max_retries=1,
        )
        text = (response.text or "").strip().strip('"')
        return text or None
    except (LLMTransientError, LLMPermanentError):
        return None
    except Exception:  # noqa: BLE001 -- an add must never fail on this
        return None


# ----------------------------------------------------------------- experience
def _find_experience(content: ResumeContent, query: str) -> int | None:
    target = _norm(query)
    if not target:
        return None
    for index, block in enumerate(content.experience):
        if _norm(block.company) == target:
            return index
    for index, block in enumerate(content.experience):
        if target in _norm(f"{block.company} {block.designation or ''}"):
            return index
    m = re.match(r"(?:experience\s*|role\s*)?(\d+)", query.strip().lower())
    if m:
        pos = int(m.group(1)) - 1
        if 0 <= pos < len(content.experience):
            return pos
    return None


def set_experience(
    content: ResumeContent,
    *,
    company_query: str,
    designation: str | None = None,
    start_label: str | None = None,
    end_label: str | None = None,
    location: str | None = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Edit one employment row: its title, its dates, its location.

    The employer itself is not editable here. Moving work between employers is a
    composition decision, not a wording one, and it belongs in the Composition
    tab where it is written to the audit log.
    """
    index = _find_experience(content, company_query)
    if index is None:
        return ToolOutcome(False, f"No employment matching '{company_query}'.")
    block = content.experience[index]
    changed = []
    if designation:
        block.designation = designation.strip()
        changed.append(f"title to '{block.designation}'")
    # As with projects: an empty string clears the field, absent leaves it.
    if start_label is not None:
        block.start_label = start_label.strip() or None
        changed.append(f"start to {block.start_label or '(cleared)'}")
    if end_label is not None:
        block.end_label = end_label.strip() or None
        changed.append(f"end to {block.end_label or '(cleared)'}")
    if location:
        block.location = location.strip()
        changed.append(f"location to {block.location}")
    if not changed:
        return ToolOutcome(False, "Nothing to change on that employment row.")
    return ToolOutcome(True, f"Updated {block.company}: set {', '.join(changed)}.")


def refresh_designation_variety(content: ResumeContent, *, context: Any = None, **kwargs: Any) -> ToolOutcome:
    """Re-title every employer from era honesty plus a seniority ladder, so a
    multi-year career does not repeat one literal title at every employer.

    This is what "the titles all look the same" / "give them more variety" /
    "vary the roles across my experience" means — a resume-wide reshuffle,
    not one row. Dates are untouched; only how each period's job title reads
    changes, climbing toward the seniority the JD's own years justify. Use
    set_experience instead for "change my title AT COMPANY X to Y" — one
    specific, named row.
    """
    from app.services import resume_composer

    if context is None or context.db is None or context.session is None:
        return ToolOutcome(False, "The knowledge base is not reachable from here.")
    if len(content.experience) < 2:
        return ToolOutcome(False, "There is only one employer on this resume — nothing to vary it against.")

    changed = resume_composer.refresh_designations(context.db, context.session, content)
    if not changed:
        return ToolOutcome(
            False,
            "Every title already reflects the seniority its period supports — there is no further "
            "variety to add without overstating an earlier role.",
        )
    titles = ", ".join(f"{b.company}: {b.designation}" for b in content.experience if b.designation)
    return ToolOutcome(True, f"Varied the titles across the experience section — {titles}.")


def rewrite_experience_highlights(
    content: ResumeContent, *, company_query: str, items: list[str], **kwargs: Any
) -> ToolOutcome:
    index = _find_experience(content, company_query)
    if index is None:
        return ToolOutcome(False, f"No employment matching '{company_query}'.")
    content.experience[index].highlights = [i.strip() for i in (items or []) if str(i).strip()]
    return ToolOutcome(True, f"Rewrote the bullets under {content.experience[index].company}.")


def remove_experience(content: ResumeContent, *, company_query: str, **kwargs: Any) -> ToolOutcome:
    index = _find_experience(content, company_query)
    if index is None:
        return ToolOutcome(False, f"No employment matching '{company_query}'.")
    removed = content.experience.pop(index)
    return ToolOutcome(True, f"Removed {removed.company} from the experience section.")


def reorder_experience(content: ResumeContent, *, order: list[str], **kwargs: Any) -> ToolOutcome:
    resolved = []
    remaining = list(content.experience)
    for query in order or []:
        index = _find_experience(ResumeContent(experience=remaining), query)
        if index is not None:
            resolved.append(remaining.pop(index))
    content.experience = resolved + remaining
    return ToolOutcome(True, "Reordered the experience section.")


# ------------------------------------------------------------------ additions
def add_education(
    content: ResumeContent,
    *,
    qualification: str,
    institution: str | None = None,
    year: str | None = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Put an education entry back on this resume.

    The house rule drops education from version 1. This is how an operator
    overrides that for one resume when a vendor asks for it.
    """
    if not (qualification or "").strip():
        return ToolOutcome(False, "An education entry needs a qualification.")
    content.education.append(
        EducationBlock(
            qualification=qualification.strip(),
            institution=(institution or "").strip() or None,
            year=(year or "").strip() or None,
        )
    )
    content.render_hints.pop("education", None)
    return ToolOutcome(True, f"Added '{qualification.strip()}' to education.")


def add_certification(content: ResumeContent, *, certification: str, **kwargs: Any) -> ToolOutcome:
    name = (certification or "").strip()
    if not name:
        return ToolOutcome(False, "A certification needs a name.")
    if any(name.lower() == c.lower() for c in content.certifications):
        return ToolOutcome(False, f"'{name}' is already listed.")
    content.certifications.append(name)
    return ToolOutcome(True, f"Added the certification '{name}'.")


def add_section(
    content: ResumeContent,
    *,
    title: str,
    body: str = "",
    items: list[str] | None = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Add a titled section the schema has no field of its own for.

    For the ones a resume occasionally needs and this platform does not model:
    Publications, Patents, Open Source, Speaking, Security Clearance. Adding
    one here changes this composition only — the person's stored record is
    untouched, which is the section 4 separation.

    An existing section of the same name is extended rather than duplicated:
    "add a publications section" twice should not print two of them.
    """
    name = (title or "").strip()
    if not name:
        return ToolOutcome(False, "A section needs a title.")

    lines = [str(i).strip() for i in (items or []) if str(i).strip()]
    text = (body or "").strip()
    if not lines and not text and not content.sections:
        # Without a content window there is nowhere for an empty section to
        # live: the renderers print the data, so a section with no data is
        # simply invisible. With a window, an empty section is a legitimate
        # request — the operator is asking for the slot, and the content
        # arrives next.
        return ToolOutcome(False, f"'{name}' would be empty — give it some content.")

    if content.sections:
        # The window is the authority: a section is added to the DESIGN, not
        # just to the data, so it prints through the template's own section
        # macro and sits wherever the operator put it.
        return ToolOutcome(*content_window.add(content, name, body=text, items=lines))

    existing = next((s for s in content.custom_sections if s.title.lower() == name.lower()), None)
    if existing is not None:
        existing.items.extend(lines)
        if text:
            existing.body = f"{existing.body} {text}".strip()
        return ToolOutcome(True, f"Added to the existing '{existing.title}' section.")

    content.custom_sections.append(CustomSection(title=name, body=text, items=lines))
    return ToolOutcome(True, f"Added a '{name}' section with {len(lines) or 1} entry/entries.")


# ───────────────────────────────────────────── reaching back into the bank
# These are the only tools that add work that is not already on the resume.
# Everything they add comes from the knowledge base — never invented — and the
# timeline is re-laid afterwards, because adding an employer changes how the
# JD's years divide across all of them.
def _resume_used_ids(content: ResumeContent) -> tuple[set[str], set[str]]:
    projects = {p.source_project_id for p in content.projects if p.source_project_id}
    companies = {e.source_company_id for e in content.experience if e.source_company_id}
    return projects, companies


def reframe_new_blocks(context: Any, content: ResumeContent, names: list[str]) -> bool:
    """Angle each newly-added project at the JD, within its period's limits.

    Runs after the timeline is re-laid, because the period is what bounds what
    the description may say. Anything that fails is left with its bank wording
    — the era check still guards the result.
    """
    from app.services import era, resume_composer

    if not names:
        return False
    releases = era.technology_release_years(context.db)
    periods = era.periods_for(content, None, releases)
    by_company = {p.company: p for p in periods}
    bound_by_company = {
        e.company: era.end_year_of(e.end_label) for e in content.experience
    }

    changed = False
    for block in content.projects:
        if block.name not in names:
            continue
        # Name the exact terms this project's own wording puts at risk, so the
        # instruction is short and specific rather than a wall of everything
        # modern.
        bound = bound_by_company.get(block.company)

        def offending(b=block, y=bound) -> list[str]:
            text = " ".join([b.description, *b.responsibilities, *b.technologies])
            return sorted({a.term for a in era.anachronisms_in(text, y, releases)})

        at_risk = offending()
        period = by_company.get(block.company)
        if resume_composer.reframe_added_project(
            context.db, context.session, block, period, at_risk
        ):
            changed = True

        # Verify, then one retry naming what it actually wrote. Forbidding
        # "RAG" and getting back "retrieval-augmented generation" is the
        # normal failure here — the model reaches for a synonym of the term it
        # was told to avoid, so the second pass has to name the synonym.
        still = offending()
        if still:
            resume_composer.reframe_added_project(
                context.db, context.session, block, period, sorted(set(at_risk) | set(still))
            )
    return changed


def update_section(
    content: ResumeContent,
    *,
    section: str,
    body: str = "",
    items: list[str] | None = None,
    **kwargs: Any,
) -> ToolOutcome:
    """REPLACE what a titled section says, rather than adding to it.

    ``add_section`` appends, which is right for "add another publication" and
    wrong for everything else: asked to reword a section, it produced the old
    text and the new text run together, and there was no way to correct it
    from the chat at all. Rewording is the commoner request of the two, so it
    gets its own tool rather than a flag on that one.

    Only the fields given are replaced, so "change the bullets" keeps the
    prose above them.
    """
    name = (section or "").strip()
    if not name:
        return ToolOutcome(False, "Which section should I change?")

    spec = content_window.find(content, name)
    lines = [str(i).strip() for i in (items or []) if str(i).strip()]
    text = (body or "").strip()
    if not text and not lines:
        return ToolOutcome(False, f"Give me the text to put in '{name}'.")

    match = next(
        (c for c in content.custom_sections if _norm(c.title) == _norm(name)), None
    )
    if match is None:
        if spec is not None and spec.kind != "custom":
            return ToolOutcome(
                False,
                f"'{spec.title}' is a built-in section, so its content comes from the "
                f"resume itself rather than free text. Edit it with the tool for that "
                f"section instead.",
            )
        # The operator asked to fill a slot that exists but has no block yet —
        # the ordinary case straight after "add a Highlights section".
        return ToolOutcome(*content_window.add(content, name, body=text, items=lines))

    if text:
        match.body = text
    if lines:
        match.items = lines
    if spec is not None and not spec.enabled:
        spec.enabled = True
    what = " and ".join(x for x in ("the text" if text else "", "the bullets" if lines else "") if x)
    return ToolOutcome(True, f"Replaced {what} in '{match.title}'.")


def add_project_from_bank(
    content: ResumeContent,
    *,
    company: str | None = None,
    count: int = 1,
    context: Any = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Add the next best-matching project from the knowledge base.

    Picks up where auto-selection stopped: if the resume took the top four,
    this adds the fifth, then the sixth. The project keeps its recorded name;
    only its stack is filtered to what the chosen employer's period can carry,
    so a project added under a 2019 role does not arrive advertising LangGraph.
    """
    from app.services import resume_composer

    if context is None or context.db is None:
        return ToolOutcome(False, "The knowledge base is not reachable from here.")
    if not content.experience:
        return ToolOutcome(False, "There are no employers on the resume to add a project under.")

    wanted = (company or "").strip().lower()
    if wanted:
        employer = next(
            (e for e in content.experience if (e.company or "").strip().lower() == wanted), None
        )
        if employer is None:
            names = ", ".join(e.company for e in content.experience)
            return ToolOutcome(False, f"'{company}' is not on this resume. Employers: {names}.")
    else:
        employer = content.experience[0]

    used_projects, _ = _resume_used_ids(content)
    ranked = resume_composer.rank_unused_projects(context.db, context.session, used_projects)
    if not ranked:
        return ToolOutcome(
            False,
            "The knowledge base has no further projects for this JD — every one is either "
            "already on this resume or was used by an approved resume for this requirement.",
        )

    added: list[str] = []
    for project in ranked[: max(1, int(count))]:
        block = resume_composer.project_block_from_bank(context.db, project)
        block.company = employer.company
        block.source_company_id = employer.source_company_id
        content.projects.append(block)
        added.append(block.name)

    notes = resume_composer.relay_timeline(context.db, context.session, content)
    reframed = reframe_new_blocks(context, content, added)
    detail = f" {notes[0]}" if notes else ""
    angle = " Descriptions were rewritten for this JD and this period." if reframed else ""
    return ToolOutcome(
        True,
        f"Added {', '.join(added)} under {employer.company} from the knowledge base, "
        f"and re-laid the timeline to keep the JD's experience total.{angle}{detail}",
    )


def add_company_from_bank(
    content: ResumeContent,
    *,
    project_count: int = 2,
    context: Any = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Add the next best-matching employer, with projects under it.

    The whole timeline is recomputed afterwards: the JD's years now divide
    across one more employer, so every date moves, and the earliest role is
    re-titled for its era.
    """
    from app.services import resume_composer

    if context is None or context.db is None:
        return ToolOutcome(False, "The knowledge base is not reachable from here.")

    used_projects, used_companies = _resume_used_ids(content)
    companies = resume_composer.rank_unused_companies(context.db, context.session, used_companies)
    if not companies:
        return ToolOutcome(
            False,
            "The knowledge base has no further employers for this JD — every one is either "
            "already on this resume or was used by an approved resume for this requirement.",
        )

    company = companies[0]
    employer = ExperienceBlock(
        source_company_id=str(company.id),
        company=company.name,
        highlights=[],
    )
    content.experience.append(employer)

    wanted = max(0, int(project_count))
    projects = resume_composer.rank_unused_projects(context.db, context.session, used_projects)
    added: list[str] = []
    for project in projects[:wanted]:
        block = resume_composer.project_block_from_bank(context.db, project)
        block.company = employer.company
        block.source_company_id = employer.source_company_id
        content.projects.append(block)
        added.append(block.name)

    resume_composer.relay_timeline(context.db, context.session, content)
    reframe_new_blocks(context, content, added)
    carried = f" with {', '.join(added)}" if added else " (no projects left in the bank to add)"
    return ToolOutcome(
        True,
        f"Added {company.name}{carried}. The timeline was re-laid across all "
        f"{len(content.experience)} employers to keep the JD's experience total, and the "
        f"earliest role re-titled for its period.",
    )


# ─────────────────────────────────────────────────── look and feel (§22)
# These change how the resume is PRESENTED, not what it says. Each writes to
# render_hints["presentation"], which travels with the version and is applied
# to both the studio preview and the compiled template — see
# app/services/presentation.py for how the override reaches the .tex.
def move_section(
    content: ResumeContent, *, section: str, direction: str = "up", **kwargs: Any
) -> ToolOutcome:
    """Shift a whole section up or down the page."""
    if content.sections:
        return ToolOutcome(*content_window.move(content, section, direction))
    changed, message = presentation.move_section(content, section, direction)
    return ToolOutcome(changed, message)


def rename_section(content: ResumeContent, *, section: str, title: str, **kwargs: Any) -> ToolOutcome:
    """Change a section's heading, keeping what fills it.

    The design's own wording is the default — a template that says "Profile"
    says Profile — but a JD that asks for "Key Achievements" over
    "Professional Experience" is a heading change, not a rewrite.
    """
    if not content.sections:
        return ToolOutcome(
            False, "This template's sections are fixed, so their headings cannot be renamed."
        )
    return ToolOutcome(*content_window.rename(content, section, title))


def show_section(content: ResumeContent, *, section: str, **kwargs: Any) -> ToolOutcome:
    """Put a hidden section back, with the content it had."""
    if not content.sections:
        return ToolOutcome(False, "This template has no hidden sections to restore.")
    return ToolOutcome(*content_window.set_enabled(content, section, True))


def set_section_order(content: ResumeContent, *, order: list[str], **kwargs: Any) -> ToolOutcome:
    """Set the top-to-bottom order of the sections outright."""
    if content.sections:
        return ToolOutcome(*content_window.reorder(content, [str(o) for o in (order or [])]))
    wanted = [str(s).strip().lower() for s in (order or []) if str(s).strip()]
    known = [s for s in wanted if s in presentation.DEFAULT_SECTION_ORDER]
    if not known:
        return ToolOutcome(
            False,
            f"None of those are sections. Available: {', '.join(presentation.DEFAULT_SECTION_ORDER)}.",
        )
    presentation.update(content, section_order=known)
    return ToolOutcome(True, f"Section order set to: {', '.join(presentation.ordered_sections(content))}.")


def set_header_alignment(content: ResumeContent, *, alignment: str, **kwargs: Any) -> ToolOutcome:
    """Move the name/contact block left, centre or right."""
    resolved = presentation.resolve_alignment(alignment)
    if resolved is None:
        return ToolOutcome(False, f"'{alignment}' is not an alignment. Use left, center or right.")
    presentation.update(content, header_align=(alignment or "").strip().lower())
    return ToolOutcome(True, f"The name and contact details are now {alignment.strip().lower()}-aligned.")


def set_accent_color(content: ResumeContent, *, color: str, **kwargs: Any) -> ToolOutcome:
    """Recolour the headings and the name."""
    resolved = presentation.resolve_color(color)
    if resolved is None:
        return ToolOutcome(
            False,
            f"'{color}' is not a colour I can use. Try a name like navy, teal or maroon, "
            f"or a hex code like #1F4E79.",
        )
    presentation.update(content, accent_color=color.strip().lower())
    return ToolOutcome(True, f"Accent colour set to {color.strip()} (#{resolved}).")


def set_font(content: ResumeContent, *, font: str, **kwargs: Any) -> ToolOutcome:
    """Change the typeface the whole document is set in."""
    if presentation.resolve_font(font) is None:
        return ToolOutcome(
            False,
            f"'{font}' is not a font I can set. Available: {', '.join(sorted(presentation.FONTS))}.",
        )
    presentation.update(content, font=font.strip().lower())
    return ToolOutcome(True, f"Typeface set to {font.strip()}.")


def set_contact_field(content: ResumeContent, *, field: str, value: str, **kwargs: Any) -> ToolOutcome:
    key = (field or "").strip().lower()
    allowed = {"full_name", "headline", "email", "phone", "location"}
    if key not in allowed:
        return ToolOutcome(False, f"'{field}' is not a contact field. Try: {', '.join(sorted(allowed))}.")
    setattr(content.contact, key, (value or "").strip())
    return ToolOutcome(True, f"Set {key.replace('_', ' ')} to '{value.strip()}'.")


def rename_skill_group(content: ResumeContent, *, label: str, new_label: str, **kwargs: Any) -> ToolOutcome:
    needle = _norm(label)
    for group in content.skill_groups:
        if _norm(group.label) == needle or (needle and needle in _norm(group.label)):
            previous = group.label
            group.label = new_label.strip()
            return ToolOutcome(True, f"Renamed the '{previous}' skill group to '{group.label}'.")
    return ToolOutcome(False, f"No skill group matching '{label}'.")


def move_skill(content: ResumeContent, *, technology: str, group_label: str, **kwargs: Any) -> ToolOutcome:
    found = _find_skill(content, technology)
    if found is None:
        return ToolOutcome(False, f"'{technology}' is not in the skills section.")
    group, index = found
    name = group.items.pop(index)
    target = None
    needle = _norm(group_label)
    for candidate in content.skill_groups:
        if _norm(candidate.label) == needle:
            target = candidate
            break
    if target is None:
        target = SkillGroup(label=group_label.strip(), tier="PRIMARY", items=[])
        content.skill_groups.append(target)
    target.items.insert(0, name)
    content.skill_groups = [g for g in content.skill_groups if g.items]
    return ToolOutcome(True, f"Moved '{name}' into '{target.label}'.")


TOOL_FUNCTIONS: dict[str, Callable[..., ToolOutcome]] = {
    "apply_next_summary_format": apply_next_summary_format,
    "rewrite_summary": rewrite_summary,
    "reorder_skill": reorder_skill,
    "remove_skill": remove_skill,
    "add_skill": add_skill,
    "reorder_skill_groups": reorder_skill_groups,
    "set_primary_project": set_primary_project,
    "reorder_projects": reorder_projects,
    "remove_project": remove_project,
    "update_project_description": update_project_description,
    "rewrite_responsibilities": rewrite_responsibilities,
    "set_render_hint": set_render_hint,
    "update_contact_headline": update_contact_headline,
    "remove_section": remove_section,
    "remove_certification": remove_certification,
    "rename_project": rename_project,
    "set_project_technologies": set_project_technologies,
    "rewrite_achievements": rewrite_achievements,
    "add_project": add_project,
    "add_section": add_section,
    "update_section": update_section,
    "add_project_from_bank": add_project_from_bank,
    "add_company_from_bank": add_company_from_bank,
    "move_section": move_section,
    "rename_section": rename_section,
    "show_section": show_section,
    "set_section_order": set_section_order,
    "set_header_alignment": set_header_alignment,
    "set_accent_color": set_accent_color,
    "set_font": set_font,
    "set_experience": set_experience,
    "refresh_designation_variety": refresh_designation_variety,
    "rewrite_experience_highlights": rewrite_experience_highlights,
    "remove_experience": remove_experience,
    "reorder_experience": reorder_experience,
    "add_education": add_education,
    "add_certification": add_certification,
    "set_contact_field": set_contact_field,
    "rename_skill_group": rename_skill_group,
    "move_skill": move_skill,
    "remove_experience_dates": remove_experience_dates,
}

#: JSON-schema tool definitions handed to the provider.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "apply_next_summary_format",
        "description": (
            "Swap the summary to the next standard FORMAT nobody has tried on this resume yet — "
            "role-led, skills-first, impact-led, career-arc, concise, or value-proposition, assembled "
            "from facts already on the resume. Takes no arguments. "
            "Use this only when the user wants a DIFFERENT ANGLE and has not said what they want "
            "instead: 'try another summary', 'I don't like this one', 'give me a different version'. "
            "Do NOT use it when the user says what is wrong with the summary — too short, too long, "
            "too generic, doesn't mention X, doesn't say enough about the person, should focus on Y. "
            "Rotating the format answers none of those, and answering 'make it longer' with a "
            "different template is the single most annoying thing this assistant can do. Use "
            "rewrite_summary for those, writing the summary yourself."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "rewrite_summary",
        "description": (
            "Rewrite the professional summary. The right tool whenever the user says what is "
            "wrong with the summary or what they want from it — longer, shorter, more specific, "
            "more about the person, focused on a particular skill, project or achievement. "
            "PASS THEIR INSTRUCTION THROUGH IN `direction`, in their own words: "
            "'make it longer and more specific', 'lead with the Azure work', 'less jargon', "
            "'emphasise the platform work over the models'. The summary is then written to that "
            "instruction from what is already on the resume, and checked before it is used. "
            "Use `text` INSTEAD only when the user dictated the actual wording, and "
            "`max_sentences` only for a plain trim of what is already there."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": (
                        "What the user wants changed, in their own words. The usual field."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "The complete new summary — only when the user dictated it.",
                },
                "max_sentences": {"type": "integer", "description": "Trim to this many sentences instead."},
            },
        },
    },
    {
        "name": "reorder_skill",
        "description": "Move a technology to a new position in the skills section. position 0 is the top.",
        "parameters": {
            "type": "object",
            "properties": {
                "technology": {"type": "string"},
                "position": {"type": "integer", "default": 0},
            },
            "required": ["technology"],
        },
    },
    {
        "name": "remove_skill",
        "description": "Remove a technology from the skills section.",
        "parameters": {
            "type": "object",
            "properties": {"technology": {"type": "string"}},
            "required": ["technology"],
        },
    },
    {
        "name": "add_skill",
        "description": (
            "Add a technology to the skills section. Only use for technologies the candidate "
            "actually has; additions not backed by the stored profile are flagged for human review."
        ),
        "parameters": {
            "type": "object",
            "properties": {"technology": {"type": "string"}, "group_label": {"type": "string"}},
            "required": ["technology"],
        },
    },
    {
        "name": "reorder_skill_groups",
        "description": "Reorder the labelled skill groups.",
        "parameters": {
            "type": "object",
            "properties": {"order": {"type": "array", "items": {"type": "string"}}},
            "required": ["order"],
        },
    },
    {
        "name": "set_primary_project",
        "description": "Move a project to first position. Match by name, keyword or 'project 2'.",
        "parameters": {
            "type": "object",
            "properties": {"project_query": {"type": "string"}},
            "required": ["project_query"],
        },
    },
    {
        "name": "reorder_projects",
        "description": "Reorder projects. Supply names or keywords in the desired order.",
        "parameters": {
            "type": "object",
            "properties": {"order": {"type": "array", "items": {"type": "string"}}},
            "required": ["order"],
        },
    },
    {
        "name": "remove_project",
        "description": "Remove a project from this resume (the master pool is untouched).",
        "parameters": {
            "type": "object",
            "properties": {"project_query": {"type": "string"}},
            "required": ["project_query"],
        },
    },
    {
        "name": "update_project_description",
        "description": "Replace a project's description with new text grounded in the stored project.",
        "parameters": {
            "type": "object",
            "properties": {"project_query": {"type": "string"}, "text": {"type": "string"}},
            "required": ["project_query", "text"],
        },
    },
    {
        "name": "rewrite_responsibilities",
        "description": "Replace a project's responsibility bullets.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_query": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["items"],
        },
    },
    {
        "name": "set_render_hint",
        "description": "Set a rendering hint, e.g. key='max_pages' value=2.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key", "value"],
        },
    },
    {
        "name": "update_contact_headline",
        "description": "Update the headline shown under the candidate's name.",
        "parameters": {
            "type": "object",
            "properties": {"headline": {"type": "string"}},
            "required": ["headline"],
        },
    },
    {
        "name": "remove_section",
        "description": (
            "Remove a whole section from this resume — 'drop the education section', "
            "'we don't want certifications here'. Name it as the resume prints it: the "
            "template's own heading works, and so does the generic name, so 'education' "
            "finds a section the design calls 'Academic Background'. The section stops "
            "printing and keeps its content, so show_section puts it back unchanged."
        ),
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}},
            "required": ["section"],
        },
    },
    {
        "name": "remove_certification",
        "description": "Remove one certification by partial name match.",
        "parameters": {
            "type": "object",
            "properties": {"certification": {"type": "string"}},
            "required": ["certification"],
        },
    },
    {
        "name": "rename_project",
        "description": (
            "Rename a project so its title speaks the target role's vocabulary. "
            "Reframe only; never rename a project into work that did not happen."
        ),
        "parameters": {
            "type": "object",
            "properties": {"project_query": {"type": "string"}, "name": {"type": "string"}},
            "required": ["project_query", "name"],
        },
    },
    {
        "name": "remove_experience_dates",
        "description": "Remove the printed dates from the experience section, one row or all.",
        "parameters": {
            "type": "object",
            "properties": {"company_query": {"type": "string"}},
        },
    },
    {
        "name": "set_project_technologies",
        "description": "Replace the technology list shown on one project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_query": {"type": "string"},
                "technologies": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_query", "technologies"],
        },
    },
    {
        "name": "rewrite_achievements",
        "description": "Replace a project's achievement bullets. Keep any numbers that were already there.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_query": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["items"],
        },
    },
    {
        "name": "add_project",
        "description": (
            "Compose a new project and add it to this resume. Use this when the user asks for a "
            "project that is not already on the resume. It affects this resume only; the "
            "candidate's stored projects are untouched. "
            "`company` is optional -- if the user does not name an employer, leave it out and "
            "the project is put under the CURRENT employer automatically. "
            "The description you write should read as work for this session's target role, using "
            "the JD's own vocabulary, and should only use technologies the JD actually asks for "
            "if they are also in `technologies` -- never invent one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "company": {
                    "type": "string",
                    "description": "Which employer, by name. Omit to use the current one.",
                },
                "role": {"type": "string"},
                "description": {"type": "string"},
                "responsibilities": {"type": "array", "items": {"type": "string"}},
                "achievements": {"type": "array", "items": {"type": "string"}},
                "technologies": {"type": "array", "items": {"type": "string"}},
                "position": {"type": "integer", "description": "0 puts it first."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_section",
        "description": (
            "Add a section to this resume by name — Publications, Patents, Open Source, "
            "Speaking, Security Clearance, anything the user asks for. It is added to the "
            "chosen TEMPLATE too, printed through that design's own section macro, so it "
            "matches everything around it. A section the design already has and that was "
            "removed earlier comes back instead of being duplicated.\n\n"
            "IF THE USER GAVE YOU THE TEXT, PASS IT. 'add a Highlights section saying X' is "
            "ONE call with title AND body — adding the empty slot and dropping X on the "
            "floor is the commonest way this goes wrong, and the user sees 'Added the "
            "section' with nothing in it. Only omit body/items when they asked for an "
            "empty section on purpose, or genuinely gave no content.\n"
            "This tool APPENDS to a section that already has content. To reword or replace "
            "what is there, use update_section. Remove it again with remove_section."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The section heading, e.g. 'Publications'."},
                "body": {"type": "string", "description": "Optional prose under the heading."},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Bullet lines for the section.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_section",
        "description": (
            "Replace what a titled section says — 'reword the Highlights section', "
            "'change Publications to ...', 'put this in the Summary of Expertise'. Use this "
            "rather than add_section whenever the user is CHANGING existing text: "
            "add_section appends, so using it to reword leaves the old and new text run "
            "together. Also the right call for filling a section that was added empty. "
            "Only the fields you pass are replaced."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "The section's heading."},
                "body": {"type": "string", "description": "Prose to put under the heading."},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Bullet lines, replacing any already there.",
                },
            },
            "required": ["section"],
        },
    },
    {
        "name": "add_project_from_bank",
        "description": (
            "Add another project to this resume from the knowledge base — 'add one more "
            "project under Accodian', 'give this company a second project'. It picks the "
            "next best-matching project for this JD that is not already on the resume, "
            "keeps its recorded name, and filters its technology list to what that "
            "employer's period can carry. The timeline is re-laid afterwards. Use this "
            "rather than add_project whenever the user wants real work from the bank; "
            "add_project is for composing something that is not on record at all."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": (
                        "Employer to add it under, exactly as printed. "
                        "Defaults to the most recent."
                    ),
                },
                "count": {"type": "integer", "description": "How many to add. Default 1."},
            },
        },
    },
    {
        "name": "add_company_from_bank",
        "description": (
            "Add another employer from the knowledge base, with projects under it — 'add "
            "one more company', 'add another company with 3 projects'. Picks the next "
            "best-matching employer for this JD, adds the requested number of projects, "
            "then re-lays the WHOLE timeline so the JD's years still divide correctly and "
            "the earliest role is titled for its era."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_count": {
                    "type": "integer",
                    "description": "Projects to put under the new employer. Default 2.",
                }
            },
        },
    },
    {
        "name": "move_section",
        "description": (
            "Move a whole section up or down the page — 'put skills above experience', "
            "'move projects to the bottom'. Name it as the resume prints it. This changes "
            "the printed resume, not just the preview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string"},
                "direction": {"type": "string", "description": "up or down"},
            },
            "required": ["section"],
        },
    },
    {
        "name": "set_section_order",
        "description": (
            "Set the whole top-to-bottom section order at once, for 'reorder the sections as "
            "summary, experience, skills, projects'. Any section left out keeps its place at "
            "the end."
        ),
        "parameters": {
            "type": "object",
            "properties": {"order": {"type": "array", "items": {"type": "string"}}},
            "required": ["order"],
        },
    },
    {
        "name": "set_header_alignment",
        "description": (
            "Move the name, email and phone block to the left, centre or right of the page. "
            "Applies to the downloaded PDF as well as the preview."
        ),
        "parameters": {
            "type": "object",
            "properties": {"alignment": {"type": "string", "description": "left, center or right"}},
            "required": ["alignment"],
        },
    },
    {
        "name": "set_accent_color",
        "description": (
            "Recolour the section headings and the candidate's name. Accepts a colour name "
            "(navy, teal, maroon, forest, purple, charcoal...) or a hex code like #1F4E79."
        ),
        "parameters": {
            "type": "object",
            "properties": {"color": {"type": "string"}},
            "required": ["color"],
        },
    },
    {
        "name": "set_font",
        "description": (
            "Change the typeface of the whole resume: sans, serif, helvetica, times, "
            "palatino, charter or mono."
        ),
        "parameters": {
            "type": "object",
            "properties": {"font": {"type": "string"}},
            "required": ["font"],
        },
    },
    {
        "name": "set_experience",
        "description": (
            "Edit ONE employment row, named by company: its job title/role/designation, its dates, "
            "its location. Use this for 'change my title at X', 'change the role at X to Y', "
            "'change X's role to Y', and 'make X run from 2019 to 2022'. Requires a specific company — "
            "for a request about the WHOLE experience section ('the roles all look the same', 'give "
            "the titles more variety'), use refresh_designation_variety instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "company_query": {"type": "string"},
                "designation": {"type": "string"},
                "start_label": {"type": "string"},
                "end_label": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["company_query"],
        },
    },
    {
        "name": "refresh_designation_variety",
        "description": (
            "Re-title EVERY employer at once from era honesty plus a seniority ladder, so a multi-year "
            "career does not repeat one literal title everywhere. Use this for 'the roles/titles all "
            "look the same', 'give the experience section more variety', 'vary the designations' — any "
            "request about the whole section, not one named company. Dates are untouched. Takes no "
            "arguments. For one specific company, use set_experience instead."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "rewrite_experience_highlights",
        "description": "Replace the bullet points shown under one employer.",
        "parameters": {
            "type": "object",
            "properties": {
                "company_query": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["company_query", "items"],
        },
    },
    {
        "name": "remove_experience",
        "description": "Remove one employer from the experience section of this resume.",
        "parameters": {
            "type": "object",
            "properties": {"company_query": {"type": "string"}},
            "required": ["company_query"],
        },
    },
    {
        "name": "reorder_experience",
        "description": "Reorder the experience section. Supply employer names in the desired order.",
        "parameters": {
            "type": "object",
            "properties": {"order": {"type": "array", "items": {"type": "string"}}},
            "required": ["order"],
        },
    },
    {
        "name": "add_education",
        "description": (
            "Add an education entry. Education is removed from every resume by default, so only "
            "call this when the user explicitly asks for it back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "qualification": {"type": "string"},
                "institution": {"type": "string"},
                "year": {"type": "string"},
            },
            "required": ["qualification"],
        },
    },
    {
        "name": "add_certification",
        "description": "Add a certification to this resume.",
        "parameters": {
            "type": "object",
            "properties": {"certification": {"type": "string"}},
            "required": ["certification"],
        },
    },
    {
        "name": "set_contact_field",
        "description": "Set one contact field: full_name, headline, email, phone or location.",
        "parameters": {
            "type": "object",
            "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
            "required": ["field", "value"],
        },
    },
    {
        "name": "rename_skill_group",
        "description": "Rename a skills row, e.g. 'Technologies' to 'Cloud & DevOps'.",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "new_label": {"type": "string"}},
            "required": ["label", "new_label"],
        },
    },
    {
        "name": "move_skill",
        "description": "Move a technology into a different skills row, creating that row if needed.",
        "parameters": {
            "type": "object",
            "properties": {"technology": {"type": "string"}, "group_label": {"type": "string"}},
            "required": ["technology", "group_label"],
        },
    },
    {
        "name": "rename_section",
        "description": (
            "Change a section's heading on this resume, keeping its content. Use the heading the "
            "resume currently shows, e.g. rename 'Professional Experience' to 'Relevant Experience'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}, "title": {"type": "string"}},
            "required": ["section", "title"],
        },
    },
    {
        "name": "show_section",
        "description": (
            "Put a section back that was removed earlier, with the content it had. Use when the user "
            "asks to restore, re-add or bring back a section this template has."
        ),
        "parameters": {
            "type": "object",
            "properties": {"section": {"type": "string"}},
            "required": ["section"],
        },
    },
]


def apply_tool(
    content: ResumeContent,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ToolContext | None = None,
) -> ToolOutcome:
    """Run one tool against the working content.

    ``context`` carries the database session for the handful of tools that
    reach back into the bank — adding another project or another employer.
    Every other tool takes ``**kwargs`` and simply ignores it, so passing it
    unconditionally keeps one dispatch path rather than two.
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return ToolOutcome(False, f"Unknown tool '{name}'.")
    try:
        return fn(content, context=context, **(arguments or {}))
    except TypeError as exc:
        return ToolOutcome(False, f"Invalid arguments for '{name}': {exc}")
