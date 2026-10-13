"""House rules applied to version 1, for every template ever uploaded.

These are not per-template settings. A new .tex arrives, nobody configures
anything, and the first version still comes out obeying the same three rules:

    1. No education section. It is dropped from the content and the LaTeX
       composer is told to delete the template's own education block.
    2. No graphical timeline bars on projects. Templates that ship progress
       bars or gantt strips render them as plain date ranges instead.
    3. The experience shown meets what the JD asks for. A 2-year profile against
       a "5+ years" JD has its employment span expanded across the companies
       already on the resume, rather than gaining a company that was not there.
    4. The resume is one page. Four or five projects, four or five employers,
       a bounded number of bullets under each and a bounded summary — the shape
       every resume this platform produces comes out in.

Rule 3 changes what the resume asserts, so it is recorded in ``render_hints``
and reported back as an applied rule — the operator sees it happened, and the
validation engine raises the one factual warning that goes with it.

Rule 3 is a default, not an enforcement. The moment an operator types dates
against an employment row in the Composition tab, ``keep_experience_dates``
switches the expansion off: a decision someone made deliberately outranks a
decision the platform made on their behalf.

Nothing in this module invents an employer, a project or a technology — it only
moves dates. Which companies and projects appear is decided in the composition
step, where the operator chooses them.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date

from app.config import (
    RESUME_MAX_BULLETS_PER_PROJECT,
    RESUME_MAX_COMPANIES,
    RESUME_MAX_DESCRIPTION_CHARS,
    RESUME_MAX_PAGES,
    RESUME_MAX_PROJECTS,
    RESUME_MAX_SKILL_GROUPS,
    RESUME_MAX_SKILLS_PER_GROUP,
    RESUME_MAX_SUMMARY_CHARS,
    RESUME_MAX_TOTAL_BULLETS,
    RULE_DROP_EDUCATION,
    RULE_MEET_JD_EXPERIENCE,
    RULE_NO_TIMELINE_BARS,
    RULE_ONE_PAGE,
)
from app.schemas.resume_content import ResumeContent

logger = logging.getLogger(__name__)

#: Anything shorter than this reads as a stint, not a job, once dates are
#: redistributed. It is the floor for a single expanded employment.
MIN_MONTHS_PER_ROLE = 6

_PRESENT = {"present", "current", "now", "till date", "to date", "ongoing"}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})


@dataclass(slots=True)
class GroundRuleResult:
    applied: list[str]
    experience_expanded_from: float | None = None
    experience_expanded_to: float | None = None


# ─────────────────────────────────────────────────────────── date handling
def _parse_label(label: str | None) -> tuple[int, int] | None:
    """A date label to ``(year, month)``. ``None`` when it says "Present"."""
    if not label:
        return None
    text = str(label).strip()
    if text.lower() in _PRESENT:
        return None

    iso = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-\d{1,2})?$", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2) or 1)

    named = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", text)
    if named:
        month = _MONTHS.get(named.group(1).lower())
        if month:
            return int(named.group(2)), month

    year = re.search(r"(19|20)\d{2}", text)
    return (int(year.group(0)), 1) if year else None


def _to_months(value: tuple[int, int]) -> int:
    return value[0] * 12 + (value[1] - 1)


def _stable_unit(key: str) -> float:
    """A repeatable 0.0-1.0 from a string.

    Deliberately not ``random``: dates that changed every time a resume was
    re-rendered would be worse than dates that are too regular.
    """
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def _separate_equal_lengths(durations: list[int], seed: str) -> list[int]:
    """Pull apart stints that came out exactly the same length.

    Weighting and rounding still collide: two employers carrying the same
    number of projects land on the same month count, and a resume showing
    21 months twice reads as computed for the same reason four 12-month rows
    did. Repeats are nudged a month at a time, and the month always comes
    from — or goes to — another role, so the JD's total is exactly what it
    was before this ran.
    """
    if len(durations) < 2:
        return durations
    spread = list(durations)
    for _ in range(len(spread) * 3):
        seen: dict[int, int] = {}
        clash: int | None = None
        for index, months in enumerate(spread):
            if months in seen:
                clash = index
                break
            seen[months] = index
        if clash is None:
            break
        step = 1 if _stable_unit(f"{seed}|separate|{clash}") < 0.5 else -1
        if spread[clash] + step < MIN_MONTHS_PER_ROLE:
            step = 1
        donors = [
            i for i in range(len(spread)) if i != clash and spread[i] - step >= MIN_MONTHS_PER_ROLE
        ]
        if not donors:
            break
        donor = max(donors, key=lambda i: spread[i])
        spread[clash] += step
        spread[donor] -= step
    return spread


def _from_months(total: int) -> tuple[int, int]:
    return total // 12, (total % 12) + 1


def _format(value: tuple[int, int]) -> str:
    return f"{calendar.month_abbr[value[1]]} {value[0]}"


def _today_months() -> int:
    today = date.today()
    return _to_months((today.year, today.month))


# ───────────────────────────────────────────────────────────────── rule 1
def drop_education(content: ResumeContent) -> bool:
    """Remove the education section outright.

    The operator's rule, not an inference: these resumes are read for delivery
    capability, and an education block is the section that most often carries a
    date that contradicts an expanded timeline.
    """
    if not content.education:
        content.render_hints["education"] = "omitted"
        return False
    content.education = []
    content.render_hints["education"] = "omitted"
    return True


# ───────────────────────────────────────────────────────────────── rule 2
def strip_timeline_bars(content: ResumeContent) -> bool:
    """Tell every renderer to print date ranges, never a bar or a gantt strip."""
    already = content.render_hints.get("timeline_bars") == "off"
    content.render_hints["timeline_bars"] = "off"
    return not already


# ───────────────────────────────────────────────────────────────── rule 3
def _span_months(block) -> int | None:
    """A block's real duration in months, or None when it cannot be read."""
    start = _parse_label(block.start_label)
    if start is None:
        return None
    end = _parse_label(block.end_label)
    end_months = _today_months() if end is None else _to_months(end)
    return max(0, end_months - _to_months(start))


def sort_experience_by_recency(content: ResumeContent) -> bool:
    """Print order follows the dates, always — most recent employer first.

    Nothing else in this module reorders ``content.experience``; every rule
    here only ever moves dates or trims counts. That was fine as long as
    whatever order the composition step produced already read newest-first,
    which it does immediately after generation. It stops being true the
    moment an operator adds one more employer in the Composition tab: the new
    row is appended at the end of the list, is dated correctly relative to
    everyone else, and then prints there too — after employers who are
    chronologically older than it. Projects are matched to an employer by id,
    not by list position, so reordering this list is always safe.
    """
    before = list(content.experience)
    content.experience.sort(key=_end_months, reverse=True)
    return content.experience != before


def _end_months(block) -> int:
    end = _parse_label(block.end_label)
    return _today_months() if end is None else _to_months(end)


def current_experience_years(content: ResumeContent) -> float | None:
    """Total years the experience section currently claims, ignoring overlaps."""
    starts, ends = [], []
    for block in content.experience:
        start = _parse_label(block.start_label)
        if start is None:
            continue
        starts.append(_to_months(start))
        end = _parse_label(block.end_label)
        ends.append(_today_months() if end is None else _to_months(end))
    if not starts:
        return None
    return round((max(ends) - min(starts)) / 12, 1)


def meet_jd_experience(content: ResumeContent, target_years: float | None) -> tuple[bool, float | None]:
    """Expand the employment timeline until it spans ``target_years``.

    Returns ``(changed, previous_years)``.

    The distribution is proportional to what each role really was, so a person
    who spent most of their career at one employer still shows that shape —
    just stretched. Roles are laid out contiguously backwards from the most
    recent end date, which is what removes the gap that naive padding creates.

    The company list is never touched. If the JD wants 8 years and the person
    lists two employers, those two employers absorb all 8.
    """
    if not target_years or not content.experience:
        return False, None

    target_months = int(round(float(target_years) * 12))
    blocks = content.experience

    spans = [_span_months(b) for b in blocks]
    known = [s for s in spans if s is not None]
    # The sum of role durations drives the weighting; the *span* is what a
    # reader totals, and the two differ whenever roles are contiguous — so the
    # decision to expand is taken on the span, not on this sum.
    current_total = sum(known)
    previous_years = current_experience_years(content)

    # A company with no dates of its own — most often one just added in the
    # Composition tab — still needs a placement even when the dated companies
    # already meet the target on their own; otherwise it never gets one.
    any_undated = any(_parse_label(b.start_label) is None for b in blocks)

    if not any_undated and previous_years is not None and previous_years >= float(target_years):
        return False, previous_years

    # Weight each role by its real length; a role with no readable dates gets an
    # average share rather than being dropped to zero.
    fallback = (current_total / len(known)) if known else 1
    weights = [float(s) if s is not None else float(fallback) for s in spans]
    if not sum(weights):
        weights = [1.0] * len(blocks)

    # Two more things shape a stint, and without them every employer came out
    # the same length: four rows reading Sep 2021-Sep 2022, Sep 2022-Sep 2023,
    # Sep 2023-Sep 2024 is arithmetic showing through the page, and the give-
    # away that nobody actually held those jobs.
    #
    #   how much work sits under it — an employer carrying three projects
    #   earned more months than one carrying a single project, and the dates
    #   should say so;
    #   a settled wobble — so no two stints land on the same round number.
    #
    # The wobble is derived from the employer, not drawn at random: the same
    # resume re-renders with the same dates, while two resumes built from the
    # same JD do not come out with matching timelines.
    per_company: dict[str, int] = {}
    for project in content.projects:
        if project.source_company_id:
            per_company[project.source_company_id] = per_company.get(project.source_company_id, 0) + 1
    seed = "|".join(block.company or "" for block in blocks)
    for index, block in enumerate(blocks):
        carried = per_company.get(block.source_company_id or "", 0)
        wobble = 0.68 + 0.64 * _stable_unit(f"{seed}|{block.company}|{index}")
        weights[index] *= (1.0 + 0.30 * carried) * wobble

    total_weight = sum(weights) or float(len(blocks))
    durations = [max(MIN_MONTHS_PER_ROLE, int(round(target_months * w / total_weight))) for w in weights]

    # Rounding and the per-role floor both drift the total; put the difference
    # on the longest role, where a month either way is invisible.
    drift = target_months - sum(durations)
    if drift:
        longest = max(range(len(durations)), key=lambda i: durations[i])
        durations[longest] = max(MIN_MONTHS_PER_ROLE, durations[longest] + drift)

    durations = _separate_equal_lengths(durations, seed)

    # Anchor on the latest end the resume already states, so a person still in
    # post keeps "Present" and one who left last year keeps that end date. Only
    # the start moves backwards.
    ends = []
    for block in blocks:
        end = _parse_label(block.end_label)
        ends.append(_today_months() if end is None else _to_months(end))
    cursor = max(ends) if ends else _today_months()
    ordered = _most_recent_first(blocks)
    for position, index in enumerate(ordered):
        block = blocks[index]
        months = durations[index]
        start = cursor - months
        keep_present = position == 0 and _parse_label(block.end_label) is None
        block.start_label = _format(_from_months(start))
        block.end_label = "Present" if keep_present else _format(_from_months(cursor))
        cursor = start

    content.render_hints["experience_years"] = float(target_years)
    # What the dates said before they were moved. Nothing else records it — no
    # career total is stored — and the validation warning needs both numbers to
    # say what this resume is now asserting.
    content.render_hints["experience_years_before"] = previous_years
    content.render_hints["experience_timeline"] = "expanded_to_jd"

    # The dates above were laid out in whatever order ``blocks`` already was,
    # which need not be newest-first any more once a company has been added —
    # see sort_experience_by_recency.
    sort_experience_by_recency(content)
    return True, previous_years


def _most_recent_first(blocks) -> list[int]:
    """Indices ordered newest first, keeping the given order when dates are absent."""
    def key(index: int):
        block = blocks[index]
        end = _parse_label(block.end_label)
        # "Present" sorts newest.
        return -(_today_months() if end is None else _to_months(end))

    return sorted(range(len(blocks)), key=key)


def normalise_date_labels(content: ResumeContent) -> int:
    """Print every experience date the same way: "Mar 2023", or "Present".

    Projects carry no dates at all (see ``app/schemas/resume_content.py``), so
    this only ever touches the experience section. Dates reach it from two
    places — an operator's override box (free text) and the timeline
    expansion. Left alone a single resume prints "2026-05" on one line and
    "May 2026" on the next, which is the sort of thing a vendor notices before
    anything else.
    """
    fixed = 0
    for block in content.experience:
        for attr in ("start_label", "end_label"):
            raw = getattr(block, attr, None)
            if not raw:
                continue
            text = str(raw).strip()
            if text.lower() in _PRESENT:
                if text != "Present":
                    setattr(block, attr, "Present")
                    fixed += 1
                continue
            parsed = _parse_label(text)
            if parsed is None:
                continue
            formatted = _format(parsed)
            if formatted != text:
                setattr(block, attr, formatted)
                fixed += 1
    return fixed


# ───────────────────────────────────────────────────────────────── rule 4
def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters without ending mid-word.

    Prefers a sentence boundary, because a description that stops at a full stop
    reads as written that way rather than as truncated. No ellipsis is added for
    the same reason. Cutting to a fixed length is idempotent, which matters:
    these rules re-run on every rebuild, and a trim that ate a little more each
    time would hollow the resume out over an afternoon of edits.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentence = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if sentence >= limit * 0.6:
        return head[: sentence + 1].strip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).strip()


def fit_one_page(
    content: ResumeContent,
    *,
    max_projects: int | None = None,
    max_companies: int | None = None,
) -> list[str]:
    """Cut the resume down to what a single page holds.

    Every resume this platform produces is one page, composed for one JD. That
    is a shape, not a preference: a vendor reads the first page and a second one
    dilutes the match rather than adding to it.

    Four decisions, in the order they cost the reader the most:

    * at most five projects and five employers — the composition already ranked
      them against the JD, so the ones cut are the ones that answer it least
    * a bounded number of bullets under each project, keeping room for one
      achievement so the metrics do not all fall off the end
    * bounded description and summary lengths
    * a bounded skills block, which is the section that silently doubles

    Returns one line per cut actually made, for the applied-rules report. It
    never reorders anything: what leads the resume was decided against the JD
    before this runs, and re-deciding it here would undo that.
    """
    notes: list[str] = []
    # The operator's chosen counts win over the house defaults; the house
    # ceiling is still the outer bound, so the form cannot ask for a resume
    # longer than the page budget allows.
    project_cap = min(max_projects or RESUME_MAX_PROJECTS, RESUME_MAX_PROJECTS)
    company_cap = min(max_companies or RESUME_MAX_COMPANIES, RESUME_MAX_COMPANIES)

    if len(content.projects) > project_cap:
        dropped = len(content.projects) - project_cap
        content.projects = content.projects[:project_cap]
        notes.append(
            f"{dropped} project(s) dropped — this resume carries at most "
            f"{project_cap}, and these matched the JD least."
        )

    if len(content.experience) > company_cap:
        dropped = len(content.experience) - company_cap
        content.experience = content.experience[:company_cap]
        notes.append(
            f"{dropped} employer(s) dropped — this resume carries at most {company_cap}."
        )

    trimmed_bullets = 0
    trimmed_text = 0
    for project in content.projects:
        before = len(project.responsibilities) + len(project.achievements)
        # Reserve one slot for an achievement where there is one: achievements
        # carry the numbers, and a bullet list with every metric cut off the
        # bottom is the weaker half of the project kept.
        reserved = 1 if project.achievements else 0
        keep_responsibilities = max(0, RESUME_MAX_BULLETS_PER_PROJECT - reserved)
        project.responsibilities = project.responsibilities[:keep_responsibilities]
        project.achievements = project.achievements[
            : max(0, RESUME_MAX_BULLETS_PER_PROJECT - len(project.responsibilities))
        ]
        trimmed_bullets += before - (len(project.responsibilities) + len(project.achievements))

        clipped = _clip(project.description, RESUME_MAX_DESCRIPTION_CHARS)
        if clipped != (project.description or "").strip():
            trimmed_text += 1
        project.description = clipped

    # A per-project cap is not a page budget: five projects at three bullets
    # each is fifteen bullets, and with a description line apiece that is more
    # than the page holds — which is exactly how the taller templates ended up
    # printing a second page. So the document also has a TOTAL, spent evenly:
    # fewer projects keep their full detail, more projects each give a little.
    total_bullets = sum(len(p.responsibilities) + len(p.achievements) for p in content.projects)
    if total_bullets > RESUME_MAX_TOTAL_BULLETS and content.projects:
        allowance = max(1, RESUME_MAX_TOTAL_BULLETS // len(content.projects))
        for project in content.projects:
            reserved = 1 if project.achievements else 0
            project.responsibilities = project.responsibilities[: max(0, allowance - reserved)]
            project.achievements = project.achievements[
                : max(0, allowance - len(project.responsibilities))
            ]
        after = sum(len(p.responsibilities) + len(p.achievements) for p in content.projects)
        trimmed_bullets += total_bullets - after
        notes.append(
            f"Bullets trimmed to {allowance} per project — {len(content.projects)} projects share "
            f"a page budget of {RESUME_MAX_TOTAL_BULLETS}."
        )

    if trimmed_bullets:
        notes.append(
            f"{trimmed_bullets} bullet(s) removed — at most "
            f"{RESUME_MAX_BULLETS_PER_PROJECT} per project on one page."
        )
    if trimmed_text:
        notes.append(f"{trimmed_text} project description(s) shortened to fit the page.")

    # Employment highlights are rarely populated, but when a studio edit adds
    # them they sit on top of the project bullets on the same page.
    for block in content.experience:
        block.highlights = block.highlights[:2]

    summary = _clip(content.summary, RESUME_MAX_SUMMARY_CHARS)
    if summary != (content.summary or "").strip():
        notes.append("Summary shortened to fit the page.")
    content.summary = summary

    if len(content.skill_groups) > RESUME_MAX_SKILL_GROUPS:
        dropped = len(content.skill_groups) - RESUME_MAX_SKILL_GROUPS
        content.skill_groups = content.skill_groups[:RESUME_MAX_SKILL_GROUPS]
        notes.append(f"{dropped} skill row(s) dropped — the page holds {RESUME_MAX_SKILL_GROUPS}.")
    for group in content.skill_groups:
        group.items = group.items[:RESUME_MAX_SKILLS_PER_GROUP]

    content.render_hints["max_pages"] = RESUME_MAX_PAGES
    return notes


# ────────────────────────────────────────────────────────────── orchestrator
def apply_v1_ground_rules(
    content: ResumeContent,
    *,
    target_experience_years: float | None = None,
    keep_experience_dates: bool = False,
    max_projects: int | None = None,
    max_companies: int | None = None,
) -> GroundRuleResult:
    """Apply every enabled house rule to a freshly generated resume.

    The one-page trim runs unconditionally on every generation and every
    rebuild: it is the shape of the document, not a step in producing the first
    version, and a studio edit that adds a sixth project would otherwise print
    onto a second page nobody reads.

    ``keep_experience_dates`` switches off the timeline expansion. It is set
    when the operator has typed dates into the Composition tab: expanding over
    those would silently discard a decision someone made deliberately, and the
    expansion is a sensible default for version 1, not a rule the platform gets
    to enforce against its user.
    """
    applied: list[str] = []
    expanded_from: float | None = None
    expanded_to: float | None = None

    if RULE_DROP_EDUCATION and drop_education(content):
        applied.append("Education section removed (house rule).")

    if RULE_NO_TIMELINE_BARS and strip_timeline_bars(content):
        applied.append("Project timeline bars disabled; dates print as plain ranges.")

    normalise_date_labels(content)

    # Runs before the timeline expansion so the span is spread across the
    # employers that will actually print, not across ones about to be cut.
    if RULE_ONE_PAGE:
        applied.extend(
            fit_one_page(content, max_projects=max_projects, max_companies=max_companies)
        )

    if RULE_MEET_JD_EXPERIENCE and target_experience_years and not keep_experience_dates:
        changed, previous = meet_jd_experience(content, target_experience_years)
        if changed:
            expanded_from, expanded_to = previous, float(target_experience_years)
            count = len(content.experience)
            applied.append(
                f"Experience timeline expanded from ~{previous or 0:g} to {target_experience_years:g} "
                f"years across the {count} compan{'y' if count == 1 else 'ies'} already on the resume."
            )
        normalise_date_labels(content)

    return GroundRuleResult(
        applied=applied,
        experience_expanded_from=expanded_from,
        experience_expanded_to=expanded_to,
    )
