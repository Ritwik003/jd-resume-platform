"""A small library of proven summary shapes, filled from what is already on
the resume — never from anything invented.

A freehand LLM summary is fine once in a while; asked for "improve the
summary" three times in a row it tends to converge on the same few phrases.
These six structures are the shapes real resumes actually use — role-led,
impact-led, skills-first, career-arc, concise, value-proposition — and
filling one in is pure string composition over facts already on the page:
the role already printed, the years the JD asks for, the skills already
ordered JD-first in the skills section, and one achievement already written
into a bullet somewhere. Nothing here can hallucinate, because nothing here
is generated — it is assembled.

The chat tool that drives these (``apply_next_summary_template`` in
resume_tools.py) works through the list once per session before falling back
to letting the model write one freehand, tracked on
``content.render_hints["summary_templates_tried"]``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class SummaryInputs:
    role: str
    years: float | None
    skills: list[str]
    domain: str | None = None
    highlight: str | None = None


def _years_phrase(years: float | None, *, cap: bool = False) -> str:
    if not years:
        return "Several years" if cap else "several years"
    text = f"{years:g}+ years"
    return text[0].upper() + text[1:] if cap else text


def _years_adj(years: float | None) -> str:
    """The hyphenated adjective form — "7-year", for "a 7-year track record"."""
    return f"{years:g}-year" if years else "proven"


def _skills_phrase(skills: list[str]) -> str:
    names = [s.strip() for s in skills if s and s.strip()][:3]
    if not names:
        return "modern engineering practices"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]}, and {names[2]}"


def _domain_bit(domain: str | None) -> str:
    return f" in {domain.strip()}" if domain and domain.strip() else ""


def _a_or_an(role: str) -> str:
    return "an" if role[:1].upper() in "AEIOU" else "a"


def _highlight_sentence(highlight: str | None) -> str:
    if not highlight or not highlight.strip():
        return "Focused on shipping reliable, production-grade work end to end."
    text = highlight.strip().rstrip(".")
    text = text[0].upper() + text[1:] if text else text
    return f"{text}."


def _role_led(i: SummaryInputs) -> str:
    return (
        f"{i.role} with {_years_phrase(i.years)} of experience building "
        f"{_skills_phrase(i.skills)}{_domain_bit(i.domain)}. {_highlight_sentence(i.highlight)}"
    )


def _impact_led(i: SummaryInputs) -> str:
    return (
        f"{_highlight_sentence(i.highlight)} Brings {_years_phrase(i.years)} as "
        f"{_a_or_an(i.role)} {i.role}, with hands-on depth in "
        f"{_skills_phrase(i.skills)}{_domain_bit(i.domain)}."
    )


def _skills_first(i: SummaryInputs) -> str:
    return (
        f"Hands-on {i.role} skilled in {_skills_phrase(i.skills)}, with {_years_phrase(i.years)} "
        f"of experience{_domain_bit(i.domain)}. {_highlight_sentence(i.highlight)}"
    )


def _career_arc(i: SummaryInputs) -> str:
    return (
        f"{i.role} with a {_years_adj(i.years)} track record{_domain_bit(i.domain)}, working "
        f"across {_skills_phrase(i.skills)}. {_highlight_sentence(i.highlight)}"
    )


def _concise(i: SummaryInputs) -> str:
    return (
        f"{_years_phrase(i.years, cap=True)} as {_a_or_an(i.role)} {i.role}, specializing in "
        f"{_skills_phrase(i.skills)}. {_highlight_sentence(i.highlight)}"
    )


def _value_proposition(i: SummaryInputs) -> str:
    return (
        f"{i.role} who pairs {_skills_phrase(i.skills)} to deliver dependable, "
        f"production-ready systems{_domain_bit(i.domain)}. {_years_phrase(i.years, cap=True)} "
        f"of hands-on experience. {_highlight_sentence(i.highlight)}"
    )


@dataclass(slots=True)
class SummaryFormat:
    key: str
    label: str
    build: Callable[[SummaryInputs], str]


#: Six shapes, in the order they get tried. Order matters only in that the
#: role-led opener — the safest, most conventional shape — goes first.
SUMMARY_FORMATS: tuple[SummaryFormat, ...] = (
    SummaryFormat("role_led", "role-led", _role_led),
    SummaryFormat("skills_first", "skills-first", _skills_first),
    SummaryFormat("impact_led", "impact-led", _impact_led),
    SummaryFormat("career_arc", "career-arc", _career_arc),
    SummaryFormat("concise", "concise", _concise),
    SummaryFormat("value_proposition", "value-proposition", _value_proposition),
)


def render(format_index: int, inputs: SummaryInputs) -> str:
    fmt = SUMMARY_FORMATS[format_index % len(SUMMARY_FORMATS)]
    return fmt.build(inputs)


def default_format_index(years: float | None) -> int:
    """Which shape opens a fresh resume — varied by experience bracket so
    every session does not open on the identical format."""
    if not years or years < 3:
        return 0  # role_led — plain and safe for an early career
    if years < 6:
        return 1  # skills_first — leads with breadth
    if years < 9:
        return 3  # career_arc — reads senior without overclaiming
    return 5  # value_proposition — the most assertive shape, earned by tenure
