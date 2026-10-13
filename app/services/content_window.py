"""The content window — one per template, seeded from that template's design.

The studio has two panes. The right one shows the chosen design; the left one
is this: the list of sections that design has, in its order and its wording,
each with the content that will go into it. Editing here is editing the resume
AND the layout at once, because the same list drives the render:

    template upload ──> blueprint ──> content.sections ──> Jinja render ──> PDF

Which is the point. Before this, the section list was a fixed set of fields
the platform believed in, and the template was a picture we asked a model to
imitate; the two agreed only by luck. Now a template that calls its summary
"Profile" offers Profile, a template with no Education section never offers
one, and a section the operator adds is printed through the design's own
section macro — so it looks like it was always part of the template.

Everything here is a pure function of ``ResumeContent``. The window travels
inside the version, so it survives a rebuild, a restore and a reopen, and
"undo" is restoring a version rather than a special case.
"""

from __future__ import annotations

import re
from typing import Any

from app.schemas.resume_content import CustomSection, ResumeContent, SectionSpec
from app.services import template_blueprint as tb

#: Titles used when a standard section is added to a design that has no block
#: for it. Only a fallback: a section that came from the design keeps the
#: design's own wording.
DEFAULT_TITLES: dict[str, str] = {
    tb.KIND_SUMMARY: "Professional Summary",
    tb.KIND_SKILLS: "Technical Skills",
    tb.KIND_EXPERIENCE: "Professional Experience",
    tb.KIND_PROJECTS: "Projects",
    tb.KIND_EDUCATION: "Education",
    tb.KIND_CERTIFICATIONS: "Certifications",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


# ─────────────────────────────────────────────────────────────────── seeding
def seed(content: ResumeContent, template: Any, *, force: bool = False) -> bool:
    """Give this resume the chosen template's section list. True if it changed.

    Called when a session is created, when its template changes, and before
    any render — so a version composed before this pipeline existed picks up
    a window the first time it is opened rather than needing a migration.

    ``force`` re-seeds from the design, discarding the operator's edits. That
    is what switching template means: the sections of the design you just left
    are not the sections of the one you just chose.
    """
    blueprint = tb.Blueprint.from_dict(getattr(template, "blueprint", None))
    if not blueprint.sections:
        return False
    if content.sections and not force:
        return False

    content.sections = [
        SectionSpec(
            key=section.key or _slug(section.title),
            title=section.title,
            kind=section.kind,
            enabled=section.enabled,
            macro=section.macro,
            added=False,
        )
        for section in blueprint.sections
    ]
    _adopt_custom_sections(content)
    return True


def blueprint(content: ResumeContent) -> tb.Blueprint:
    """The window as the renderer wants it."""
    return tb.Blueprint.from_dict({"sections": [s.model_dump() for s in content.sections]})


def _adopt_custom_sections(content: ResumeContent) -> None:
    """Any operator-added block with no row of its own gets one, at the end."""
    known = {_slug(s.title) for s in content.sections}
    for custom in content.custom_sections:
        if custom.title and _slug(custom.title) not in known:
            content.sections.append(
                SectionSpec(
                    key=_slug(custom.title),
                    title=custom.title,
                    kind=tb.KIND_CUSTOM,
                    enabled=True,
                    macro=_house_macro(content),
                    added=True,
                )
            )
            known.add(_slug(custom.title))


def _house_macro(content: ResumeContent) -> str | None:
    r"""How this design opens a section, e.g. ``\section`` or ``\cvsection``.

    Taken from the design's own sections so an added one is printed the same
    way. ``None`` when the design gave us nothing to copy, in which case the
    template's ``sections`` loop falls back to its own default.
    """
    for spec in content.sections:
        if spec.macro:
            return spec.macro
    return None


# ──────────────────────────────────────────────────────────────── lookup
def find(content: ResumeContent, needle: str) -> SectionSpec | None:
    """The section an operator means by ``needle``.

    Deliberately forgiving: they will type the design's heading ("Academic
    Background"), the generic name ("education"), or the plural of either.
    """
    want = _slug(needle)
    if not want:
        return None
    singular = want.rstrip("s")
    for spec in content.sections:
        if want in (spec.key, _slug(spec.title)) or singular == _slug(spec.title).rstrip("s"):
            return spec
    for spec in content.sections:
        if spec.kind == want or spec.kind == singular:
            return spec
    # Last resort: the kind the WORDS point at, so "remove the summary" finds
    # a section the design happens to call "Profile".
    kind = tb.classify(needle)
    if kind != tb.KIND_CUSTOM:
        return next((s for s in content.sections if s.kind == kind), None)
    return None


def titles(content: ResumeContent) -> str:
    return ", ".join(s.title for s in content.sections) or "(none)"


# ─────────────────────────────────────────────────────────────── operations
#
# Each returns ``(changed, message)`` — the shape the chat tools speak, so a
# studio click and a typed instruction go through exactly the same code and
# cannot diverge.


def set_enabled(content: ResumeContent, needle: str, enabled: bool) -> tuple[bool, str]:
    spec = find(content, needle)
    if spec is None:
        return False, f"This template has no '{needle}' section. It has: {titles(content)}."
    if spec.enabled == enabled:
        state = "already shown" if enabled else "already hidden"
        return False, f"'{spec.title}' is {state}."
    spec.enabled = enabled
    return True, f"{'Restored' if enabled else 'Removed'} the '{spec.title}' section."


def remove(content: ResumeContent, needle: str, *, purge: bool = False) -> tuple[bool, str]:
    """Take a section off the resume.

    The row stays, disabled, and its content stays with it: "remove education"
    and "put education back" should not cost a regeneration. ``purge`` drops
    the row outright, which is right only for a section the operator added
    here and now wants gone.
    """
    spec = find(content, needle)
    if spec is None:
        return False, f"This template has no '{needle}' section. It has: {titles(content)}."
    if purge or spec.added:
        content.sections = [s for s in content.sections if s is not spec]
        if spec.kind == tb.KIND_CUSTOM:
            content.custom_sections = [
                c for c in content.custom_sections if _slug(c.title) != _slug(spec.title)
            ]
        return True, f"Removed the '{spec.title}' section."
    return set_enabled(content, needle, False)


def add(
    content: ResumeContent,
    title: str,
    *,
    kind: str | None = None,
    body: str = "",
    items: list[str] | None = None,
    position: int | None = None,
) -> tuple[bool, str]:
    """Put a section on the resume, whether or not the design has one.

    Three cases, and all three are ordinary:

    * the design has it and it is hidden — it comes back, with its content;
    * the design has it and it is showing — nothing to do;
    * the design has no such section — a row is appended that prints through
      the design's own section macro, so it matches everything around it.

    A section added with no content is fine and is not an error: the operator
    asked for the *slot*, and the content arrives next, from the chat or from
    the generator.
    """
    name = (title or "").strip()
    if not name:
        return False, "A section needs a title."

    existing = find(content, name)
    resolved = kind or tb.classify(name)

    if existing is not None:
        changed = False
        if not existing.enabled:
            existing.enabled = True
            changed = True
        if existing.kind == tb.KIND_CUSTOM and (body or items):
            _upsert_custom(content, existing.title, body, items)
            changed = True
        if changed:
            return True, f"Added the '{existing.title}' section."
        return False, f"'{existing.title}' is already on this resume."

    spec = SectionSpec(
        key=_slug(name),
        title=name,
        kind=resolved,
        enabled=True,
        macro=_house_macro(content),
        added=True,
    )
    if position is None or not 0 <= position <= len(content.sections):
        content.sections.append(spec)
    else:
        content.sections.insert(position, spec)

    if resolved == tb.KIND_CUSTOM:
        _upsert_custom(content, name, body, items)

    return True, f"Added a '{name}' section to this template."


def _upsert_custom(content: ResumeContent, title: str, body: str, items: list[str] | None) -> None:
    lines = [str(i).strip() for i in (items or []) if str(i).strip()]
    text = (body or "").strip()
    match = next((c for c in content.custom_sections if _slug(c.title) == _slug(title)), None)
    if match is None:
        content.custom_sections.append(CustomSection(title=title, body=text, items=lines))
        return
    if text:
        match.body = f"{match.body} {text}".strip() if match.body else text
    match.items.extend(lines)


def rename(content: ResumeContent, needle: str, new_title: str) -> tuple[bool, str]:
    """Change a section's heading without changing what fills it."""
    name = (new_title or "").strip()
    if not name:
        return False, "A section needs a title."
    spec = find(content, needle)
    if spec is None:
        return False, f"This template has no '{needle}' section. It has: {titles(content)}."
    was = spec.title
    if _slug(was) == _slug(name):
        return False, f"'{was}' is already called that."
    if spec.kind == tb.KIND_CUSTOM:
        # The custom block is matched to its row by title, so the content has
        # to follow the heading or it stops printing.
        for custom in content.custom_sections:
            if _slug(custom.title) == _slug(was):
                custom.title = name
    spec.title = name
    return True, f"Renamed '{was}' to '{name}'."


def reorder(content: ResumeContent, order: list[str]) -> tuple[bool, str]:
    """Set the top-to-bottom order. Sections not named keep their order, after."""
    if not content.sections:
        return False, "This template has no section list yet."
    picked: list[SectionSpec] = []
    for needle in order or []:
        spec = find(content, str(needle))
        if spec is not None and spec not in picked:
            picked.append(spec)
    if not picked:
        return False, f"None of those are sections of this template. It has: {titles(content)}."
    rest = [s for s in content.sections if s not in picked]
    content.sections = picked + rest
    return True, f"Section order is now: {titles(content)}."


def move(content: ResumeContent, needle: str, direction: str = "up") -> tuple[bool, str]:
    spec = find(content, needle)
    if spec is None:
        return False, f"This template has no '{needle}' section. It has: {titles(content)}."
    index = content.sections.index(spec)
    target = index - 1 if str(direction).lower().startswith("u") else index + 1
    if not 0 <= target < len(content.sections):
        edge = "top" if target < 0 else "bottom"
        return False, f"'{spec.title}' is already at the {edge}."
    content.sections[index], content.sections[target] = content.sections[target], content.sections[index]
    return True, f"Moved '{spec.title}' {'up' if target < index else 'down'}."


# ────────────────────────────────────────────────────────── what the UI shows
#: How much content each kind currently has, so the window can show a section
#: as filled or empty without the client knowing the content schema.
def _fill(content: ResumeContent, kind: str, title: str) -> tuple[int, str]:
    if kind == tb.KIND_SUMMARY:
        words = len((content.summary or "").split())
        return words, f"{words} words" if words else "empty"
    if kind == tb.KIND_SKILLS:
        n = sum(len(g.items) for g in content.skill_groups)
        return n, f"{n} skills in {len(content.skill_groups)} groups" if n else "empty"
    if kind == tb.KIND_EXPERIENCE:
        n = len(content.experience)
        return n, f"{n} employers" if n else "empty"
    if kind == tb.KIND_PROJECTS:
        n = len(content.projects)
        return n, f"{n} projects" if n else "empty"
    if kind == tb.KIND_EDUCATION:
        n = len(content.education)
        return n, f"{n} entries" if n else "empty"
    if kind == tb.KIND_CERTIFICATIONS:
        n = len(content.certifications)
        return n, f"{n} items" if n else "empty"
    match = next((c for c in content.custom_sections if _slug(c.title) == _slug(title)), None)
    if match is None:
        return 0, "empty"
    n = len(match.items) + (1 if match.body else 0)
    return n, f"{len(match.items)} bullets" if match.items else ("prose" if match.body else "empty")


def describe(content: ResumeContent) -> dict[str, Any]:
    """The content window as the studio renders it.

    ``sections`` is the window itself. ``unused`` is the honest other half:
    content this resume carries that the chosen design has nowhere to print —
    a candidate with education and a template with no Education section. Rather
    than dropping it silently or forcing it in, the window shows it with an
    offer to add the section, and the operator decides.
    """
    rows: list[dict[str, Any]] = []
    for spec in content.sections:
        count, label = _fill(content, spec.kind, spec.title)
        rows.append(
            {
                **spec.model_dump(),
                "content_count": count,
                "content_label": label,
                "empty": count == 0,
            }
        )

    present = {s.kind for s in content.sections}
    unused: list[dict[str, Any]] = []
    for kind in tb.KNOWN_KINDS:
        if kind in present:
            continue
        count, label = _fill(content, kind, "")
        if count:
            unused.append({"kind": kind, "suggested_title": DEFAULT_TITLES[kind], "content_label": label})

    return {"sections": rows, "unused": unused}
