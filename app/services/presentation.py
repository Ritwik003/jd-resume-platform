"""Look-and-feel the operator controls from the chat, not from the template.

Section order, header alignment, accent colour and typeface are decisions
about how a resume *looks*, and they were previously locked inside whichever
.tex was uploaded — changing one meant editing the template, which changes it
for everybody. They belong to the composition instead.

The settings live in ``ResumeContent.render_hints["presentation"]`` so they
travel with the version: they survive a rebuild, a restore, and a reopen, and
the studio preview and the compiled PDF read the same values rather than
drifting apart.

How they reach the PDF: the template's preamble is copied verbatim (see
``latex_composer``), and a small override block is appended to it just before
``\\begin{document}``. LaTeX takes the last definition, so redefining
``\\accentcolour`` or ``\\familydefault`` there wins over the template's own
without the template being edited or reproduced. Section ORDER cannot work
that way — it is the body, which the model writes — so that is passed as an
instruction instead.
"""

from __future__ import annotations

import re
from typing import Any

from app.schemas.resume_content import ResumeContent

#: Colour words an operator would actually type, resolved to hex. Anything
#: outside this list is accepted as a raw #RRGGBB, so the palette is a
#: convenience rather than a restriction.
COLORS: dict[str, str] = {
    "navy": "1F4E79",
    "blue": "1F4E79",
    "royal blue": "2B5CB8",
    "teal": "0F6466",
    "green": "1B5E20",
    "forest": "1B5E20",
    "maroon": "7B1E28",
    "burgundy": "7B1E28",
    "red": "A32020",
    "orange": "B5511F",
    "purple": "5B2C82",
    "charcoal": "333333",
    "black": "000000",
    "grey": "444444",
    "gray": "444444",
}

#: Typeface families every TeX installation has, so a font change never fails
#: to compile. "sans" and "serif" switch the default family; the named ones
#: additionally load their package.
FONTS: dict[str, str] = {
    # name: the OpenType family, loaded by FILENAME (tectonic cannot resolve
    # these by family name) and always with ligatures and kerning switched off.
    #
    # These used to be Type1 packages — "helvetica" loaded {helvet} and
    # switched \\familydefault. That silently undid the ATS font setup the
    # designs carry: ff collapses back to one glyph so "Staff" stops matching,
    # and a wide kern reads as a space so "FinTech" becomes "FinT ech". Asking
    # the assistant for a nicer typeface should not cost the candidate keywords
    # in a parser, so an override now switches the face the SAME way the
    # templates set it in the first place.
    "sans": "texgyreheros",
    "sans-serif": "texgyreheros",
    "helvetica": "texgyreheros",
    "arial": "texgyreheros",
    "serif": "texgyretermes",
    "times": "texgyretermes",
    "roman": "texgyretermes",
    "palatino": "texgyrepagella",
    "charter": "texgyrebonum",
    "bookman": "texgyrebonum",
    "schoolbook": "texgyreschola",
    "avantgarde": "texgyreadventor",
    # Courier clone. Latin Modern Mono was the obvious pick and does not ship a
    # matching bold/italic under a predictable filename, so the compile died on
    # the face it could not find.
    "mono": "texgyrecursor",
    "monospace": "texgyrecursor",
    "courier": "texgyrecursor",
}

#: Latin Modern names its slanted faces "oblique"; the TeX Gyre families use
#: "italic". Anything not listed here follows the TeX Gyre convention.
_FONT_FACES: dict[str, tuple[str, str, str, str]] = {
    "lmsans10": ("regular", "bold", "oblique", "boldoblique"),
    "lmroman10": ("regular", "bold", "italic", "bolditalic"),
}

ALIGNMENTS = {"left": "raggedright", "center": "centering", "centre": "centering", "right": "raggedleft"}

#: The sections a resume can carry, in the order they print by default.
DEFAULT_SECTION_ORDER = ["summary", "skills", "experience", "projects", "certifications"]

_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def get(content: ResumeContent) -> dict[str, Any]:
    """This resume's presentation settings, with the defaults filled in."""
    stored = content.render_hints.get("presentation")
    settings: dict[str, Any] = {
        "header_align": "center",
        "accent_color": None,
        "font": None,
        "section_order": None,
    }
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in settings})
    return settings


def update(content: ResumeContent, **changes: Any) -> dict[str, Any]:
    settings = get(content)
    settings.update(changes)
    content.render_hints["presentation"] = settings
    return settings


def resolve_color(value: str) -> str | None:
    """A colour word or hex code to six hex digits, or ``None`` if unusable."""
    text = (value or "").strip().lower()
    if text in COLORS:
        return COLORS[text]
    match = _HEX.match(text)
    return match.group(1).upper() if match else None


def resolve_font(value: str) -> str | None:
    return FONTS.get((value or "").strip().lower())


def font_latex(family: str) -> str:
    """The fontspec block that selects one family, ATS-safely."""
    reg, bold, ital, bi = _FONT_FACES.get(
        family, ("regular", "bold", "italic", "bolditalic")
    )
    return (
        "\\usepackage{fontspec}\n"
        f"\\setmainfont{{{family}-{reg}.otf}}[\n"
        f"  BoldFont={family}-{bold}.otf,\n"
        f"  ItalicFont={family}-{ital}.otf,\n"
        f"  BoldItalicFont={family}-{bi}.otf,\n"
        "  Ligatures=NoCommon,\n"
        "  Kerning=Off]"
    )


def resolve_alignment(value: str) -> str | None:
    return ALIGNMENTS.get((value or "").strip().lower())


def ordered_sections(content: ResumeContent) -> list[str]:
    """The section order this resume asks for, defaults filled in.

    Any section the operator did not mention keeps its default position at the
    end, so reordering two sections never silently drops a third.
    """
    wanted = get(content).get("section_order")
    if not isinstance(wanted, list) or not wanted:
        return list(DEFAULT_SECTION_ORDER)
    seen = [s for s in wanted if s in DEFAULT_SECTION_ORDER]
    return seen + [s for s in DEFAULT_SECTION_ORDER if s not in seen]


def move_section(content: ResumeContent, section: str, direction: str) -> tuple[bool, str]:
    """Shift one section up or down by a place. Returns ``(changed, message)``."""
    key = (section or "").strip().lower().rstrip("s")
    canonical = next(
        (s for s in DEFAULT_SECTION_ORDER if s.rstrip("s") == key or s.startswith(key)), None
    )
    if canonical is None:
        return False, (
            f"'{section}' is not a section that can be moved. "
            f"Movable: {', '.join(DEFAULT_SECTION_ORDER)}."
        )

    order = ordered_sections(content)
    index = order.index(canonical)
    step = -1 if direction.strip().lower() in ("up", "above", "earlier", "before") else 1
    target = index + step
    if not 0 <= target < len(order):
        return False, f"'{canonical}' is already {'first' if step < 0 else 'last'}."

    order[index], order[target] = order[target], order[index]
    update(content, section_order=order)
    return True, f"Moved '{canonical}' {'up' if step < 0 else 'down'}; order is now {', '.join(order)}."


# ─────────────────────────────────────────────────────── reaching the PDF
def latex_overrides(content: ResumeContent) -> str:
    r"""The block appended to the template's preamble.

    Everything here is a redefinition of something the template already
    defined, so LaTeX's last-one-wins is what makes it take effect without the
    template being rewritten. It is deliberately small: a colour, a family, an
    alignment. Anything larger would be reproducing the design rather than
    adjusting it.
    """
    settings = get(content)
    lines: list[str] = []

    family = resolve_font(settings.get("font") or "")
    if family is not None:
        # Appended after the design's own \setmainfont, so this one wins —
        # and because it is the same shape of declaration, it wins WITH the
        # ligature and kerning settings rather than without them.
        lines.append(font_latex(family))

    color = resolve_color(settings.get("accent_color") or "")
    if color:
        # \providecolor first so this works even on a template that never
        # defined "accent" — \definecolor alone is fine, but being explicit
        # documents that the name is ours, not the template author's.
        lines.append(f"\\definecolor{{accent}}{{HTML}}{{{color}}}")

    align = resolve_alignment(settings.get("header_align") or "")
    if align and align != "centering":
        lines.append(f"\\renewcommand{{\\headeralign}}{{\\{align}}}")

    if not lines:
        return ""
    return (
        "\n% ---- studio presentation overrides (set from the AI Assistant) ----\n"
        + "\n".join(lines)
        + "\n"
    )


def house_rule(content: ResumeContent) -> str | None:
    """What to tell the LaTeX composer about order and alignment."""
    settings = get(content)
    parts: list[str] = []
    order = settings.get("section_order")
    if isinstance(order, list) and order:
        parts.append(
            "print the sections in exactly this order, top to bottom: " + ", ".join(order)
        )
    align = settings.get("header_align")
    if align and align != "center":
        parts.append(
            f"the name/contact header is {align}-aligned - the preamble already sets this "
            f"via \\headeralign, so use that macro rather than hard-coding \\centering"
        )
    return "; ".join(parts) if parts else None
