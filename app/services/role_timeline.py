"""What a job title was called before it existed — resolved, not tabulated.

``era.py`` used to carry two hand-written tables: a map of job titles to the
year they became titles, and a ladder of earlier equivalents per discipline.
Both had the same flaw. They only knew the roles somebody had thought to add,
so a JD for a role outside the list was dated by a default and quietly waved
through; and they could not see how a particular advert words its own role, so
"AI Engineer (Azure)" was treated as a title distinct from "AI Engineer".

This module derives the answer for the role actually in front of us and stores
it, keyed on the role. The cost is one model call per distinct role the
platform ever sees — not one per resume — and a role nobody anticipated is
handled correctly the first time it arrives.

What comes back per role:

  discipline   the job, with the advert's stack qualifier and seniority word
               stripped ("AI Engineer (Azure)" -> "AI Engineer")
  emerged      the year that title became a common job title
  ladder       the honest earlier names for the same career, oldest first,
               each with the year it in turn became common
  seniority    the wording this discipline actually uses to climb

When the model cannot be reached the resolver returns an UNRESOLVED timeline,
and every caller then declines to act rather than guessing: no demotion, no
seniority ladder, the advert's own role printed as given. That is the safe
direction — a resume that fails to step a title back is ordinary, while one
that steps it back to a title this discipline never used is a new kind of
wrong, and inventing a fallback table here would be the very thing this module
exists to remove.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS
from app.models.role_timeline import RoleTimeline
from app.prompts.templates import ROLE_TIMELINE_SYSTEM, ROLE_TIMELINE_USER
from app.services.llm.base import LLMPermanentError, LLMTransientError
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
#: Anything a person could plausibly have been called. Rejects a model reply
#: that returns a sentence, a stack name or an empty string as a "title".
_PLAUSIBLE_TITLE = re.compile(r"^[A-Za-z][A-Za-z0-9 .&/+'\-]{1,60}$")
#: The window a job title can sensibly have appeared in.
_EARLIEST, _LATEST = 1970, 2100
#: Every spelling of the placeholder a model reaches for, braces included.
_PLACEHOLDER = re.compile(r"\{{1,2}\s*role\s*\}{1,2}|<\s*role\s*>", re.IGNORECASE)


def normalise(role: str | None) -> str:
    """The cache key: one row per role however the advert punctuated it."""
    return re.sub(r"[^a-z0-9]+", " ", (role or "").lower()).strip()


@dataclass(slots=True)
class Rung:
    title: str
    emerged: int | None


@dataclass(slots=True)
class Timeline:
    """One role's dating, or an explicitly unresolved one."""

    role: str = ""
    discipline: str = ""
    emerged: int | None = None
    ladder: list[Rung] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    resolved: bool = False

    def title_for(self, start_year: int | None) -> str:
        """The title honestly held from ``start_year``, stepped back if needed.

        Returns the discipline whenever the year supports it — the normal case
        for recent jobs, and the whole point of moulding a resume to a JD.
        Unresolved, or undated, it returns the discipline unchanged: this
        declines to demote rather than guessing at a demotion.
        """
        base = self.discipline or self.role
        if not self.resolved or start_year is None or self.emerged is None:
            return base
        if start_year >= self.emerged:
            return base
        earlier = [r for r in self.ladder if r.emerged is not None and r.emerged <= start_year]
        return earlier[-1].title if earlier else base

    def climb(self, count: int, ceiling: int | None = None) -> list[str]:
        """``count`` titles, OLDEST FIRST, climbing toward the present.

        Counted down from the top rung the years justify, so where there are
        more employers than rungs the repeat lands on the OLDEST jobs — two
        early years under one title is ordinary, two recent ones is the first
        thing a reader notices.
        """
        base = self.discipline or self.role
        if not self.resolved or not self.seniority or count <= 0:
            return [base] * max(0, count)
        top = len(self.seniority) if ceiling is None else max(1, min(ceiling, len(self.seniority)))
        rungs = [max(0, top - 1 - k) for k in range(count)][::-1]
        return [_fill(self.seniority[r], base) for r in rungs]

    def era_titles(self, start_year: int | None) -> list[str]:
        """Every honest name for this career that existed by ``start_year``.

        Oldest first, the discipline itself last. These are the resolver's own
        ladder rungs, so each is a title this career genuinely passed through
        and each carries the year it became common — nothing is invented here.
        """
        base = self.discipline or self.role
        if not self.resolved:
            return [base]
        usable = [
            rung.title
            for rung in self.ladder
            if rung.emerged is not None and (start_year is None or rung.emerged <= start_year)
        ]
        # The discipline is only one of the options when the year had actually
        # reached it — offering "AI/ML Engineer" to a 2020 employment is the
        # exact claim this module exists to prevent.
        if start_year is None or self.emerged is None or start_year >= self.emerged:
            usable.append(base)
        titles: list[str] = []
        for title in usable:
            if title and title not in titles:
                titles.append(title)
        return titles or [base]

    def varied_climb(
        self, start_years: list[int | None], ceiling: int | None = None, seed: str = ""
    ) -> list[str]:
        """Titles for a run of employments, OLDEST FIRST, that read as a career.

        ``climb`` walks ONE noun up a seniority ladder. That is honest, and it
        is also unmistakably generated: four employers print "X", "X II",
        "Senior X", "Staff X" — the same noun four times with a different word
        bolted on the front. Real careers move sideways as well as up, because
        employers name the same job differently and because the job itself was
        called something else a few years ago.

        The sideways names are not invented here either: they are this
        discipline's own ladder rungs, each already dated by the resolver, so a
        rung is only offered to an employment whose start year it had actually
        reached. How far back a given position may reach grows with its age,
        and the choice among the era-valid names is deterministic in ``seed``
        — the same resume re-renders identically, two different resumes do not
        share a pattern.

        The most recent employment always keeps the JD's own discipline. That
        is the title being read against the advert, and the one place variety
        would cost something.
        """
        count = len(start_years)
        base = self.discipline or self.role
        if not self.resolved or count <= 0:
            return [base] * max(0, count)

        if self.seniority:
            top_rung = len(self.seniority) if ceiling is None else max(1, min(ceiling, len(self.seniority)))
            rungs = [max(0, top_rung - 1 - k) for k in range(count)][::-1]
        else:
            rungs = [0] * count

        # The career as one ordered chain, oldest name first, each with the
        # year it became common. Working in positions along this chain rather
        # than in strings is what keeps the run monotonic below.
        chain: list[tuple[str, int | None]] = []
        for rung in self.ladder:
            if rung.title and rung.emerged is not None:
                chain.append((rung.title, rung.emerged))
        if not any(title == base for title, _ in chain):
            chain.append((base, self.emerged))
        seen: set[str] = set()
        chain = [(t, y) for t, y in chain if not (t in seen or seen.add(t))]

        def furthest_by(year: int | None) -> int:
            reached = [
                i
                for i, (_, emerged) in enumerate(chain)
                if year is None or emerged is None or emerged <= year
            ]
            return reached[-1] if reached else 0

        def name_at(position: int, index: int) -> str:
            noun = chain[position][0]
            return _fill(self.seniority[rungs[index]], noun) if self.seniority else noun

        titles: list[str] = []
        # A career does not walk backwards, so a pick never precedes the last.
        floor = 0
        for index, year in enumerate(start_years):
            from_recent = count - 1 - index  # 0 is the most recent employment
            ceiling_here = furthest_by(year)
            if from_recent == 0:
                chosen = ceiling_here
            else:
                # Reach one name back for the job before this one, two for
                # anything older — never past what the year supports, and
                # never behind where the previous (older) job already stood.
                lowest = max(floor, ceiling_here - (1 if from_recent == 1 else 2))
                chosen = lowest + _stable_index(f"{seed}|{index}|{year}", ceiling_here - lowest + 1)
            chosen = max(chosen, floor)
            title = name_at(chosen, index)
            # An adjacent exact repeat is the thing this is trying to avoid, so
            # step forward one name where the year still allows it.
            if titles and title == titles[-1] and chosen < ceiling_here:
                chosen += 1
                title = name_at(chosen, index)
            floor = chosen
            titles.append(title)
        return titles


def _stable_index(key: str, size: int) -> int:
    """A repeatable choice in ``range(size)`` — same key, same answer, always.

    Deliberately not ``random``: a resume that re-renders with different job
    titles every time it is opened is worse than a predictable one.
    """
    if size <= 1:
        return 0
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16) % size


def _fill(template: str, role: str) -> str:
    """Put the discipline into a seniority template, however it was written.

    Models return the placeholder as ``{role}``, ``{{role}}`` or ``<role>``
    depending on how the instruction was read, and a template that survives
    only one of those spellings prints "{Data Engineer}" on somebody's resume.
    """
    filled = _PLACEHOLDER.sub(role, template).strip()
    return filled or role


def ceiling_for(target_years: float | None) -> int:
    """How far up the seniority wording the JD's own stated years justify.

    A policy about years, not a table of titles: the JD asks for three years,
    so the resume does not open on a Lead title however many employers it
    lists. The wording being indexed into came from the resolver.
    """
    if not target_years or target_years < 3:
        return 1
    if target_years < 5:
        return 2
    if target_years < 8:
        return 3
    return 4


def _unresolved(role: str) -> Timeline:
    plain = (role or "").strip()
    return Timeline(role=plain, discipline=plain, resolved=False)


def _year(value: object) -> int | None:
    try:
        year = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return year if _EARLIEST <= year <= _LATEST else None


def _from_row(row: RoleTimeline) -> Timeline:
    ladder = [
        Rung(title=str(r.get("title") or ""), emerged=_year(r.get("emerged")))
        for r in (row.ladder or [])
        if isinstance(r, dict) and r.get("title")
    ]
    return Timeline(
        role=row.role_text,
        discipline=row.discipline,
        emerged=row.emerged,
        ladder=ladder,
        seniority=[str(t) for t in (row.seniority or []) if str(t).strip()],
        resolved=True,
    )


def _parse(text: str, role: str) -> Timeline | None:
    """The model's reply, or ``None`` when it is not usable.

    Deliberately strict. A timeline is used to decide what a resume may claim
    about somebody's past, so a half-parsed answer is worse than none: the
    caller's unresolved path is safe, a malformed ladder is not.
    """
    try:
        payload = json.loads(_FENCE.sub("", text or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    discipline = str(payload.get("discipline") or "").strip()
    if not _PLAUSIBLE_TITLE.match(discipline):
        return None

    ladder: list[Rung] = []
    for raw in payload.get("ladder") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if _PLAUSIBLE_TITLE.match(title):
            ladder.append(Rung(title=title, emerged=_year(raw.get("emerged"))))
    # Oldest first is what title_for walks; sort defensively rather than
    # trusting the order, and drop rungs the model left undated because an
    # undated rung cannot answer "was this title available in 2019".
    ladder = sorted((r for r in ladder if r.emerged is not None), key=lambda r: r.emerged or 0)

    seniority = [str(t).strip() for t in payload.get("seniority") or [] if str(t).strip()]
    # Every rung has to be a template, or the climb prints the same title
    # repeatedly without anybody noticing.
    seniority = [t for t in seniority if _PLACEHOLDER.search(t)]

    return Timeline(
        role=role,
        discipline=discipline,
        emerged=_year(payload.get("emerged")),
        ladder=ladder,
        seniority=seniority,
        resolved=True,
    )


def resolve(db: Session, role: str | None) -> Timeline:
    """This role's timeline, from the store or from the model once.

    Never raises: a provider that is down returns an unresolved timeline, and
    the callers treat that as "do not step anything back", which keeps a
    resume composable when the dating service is not available.
    """
    key = normalise(role)
    if not key:
        return _unresolved(role or "")

    row = db.query(RoleTimeline).filter(RoleTimeline.role_key == key).first()
    if row is not None:
        return _from_row(row)

    try:
        provider = get_llm_provider()
        response = with_retry(
            lambda: provider.chat(
                [{"role": "user", "content": ROLE_TIMELINE_USER.format(role=role)}],
                model=LLM_MODEL,
                system=ROLE_TIMELINE_SYSTEM,
                temperature=LLM_TEMPERATURE,
                # A reasoning model spends most of a small budget thinking
                # and returns an empty string; measured on this provider, a
                # 2000-token ceiling produced nothing at all for two roles
                # out of three. This answer is short — the room is for the
                # thinking in front of it.
                max_tokens=max(LLM_MAX_TOKENS, 16000),
                timeout=LLM_TIMEOUT_SECONDS,
            ),
            max_retries=1,
        )
    except (LLMTransientError, LLMPermanentError) as exc:
        logger.warning("Could not date the role %r: %s", role, exc)
        return _unresolved(role or "")
    except Exception:  # noqa: BLE001 — dating a role must never sink a compose
        logger.exception("Dating the role %r failed", role)
        return _unresolved(role or "")

    timeline = _parse(response.text or "", role or "")
    if timeline is None:
        logger.warning("The role timeline for %r came back unusable", role)
        return _unresolved(role or "")

    db.add(
        RoleTimeline(
            role_key=key,
            role_text=(role or "").strip()[:255],
            discipline=timeline.discipline[:255],
            emerged=timeline.emerged,
            ladder=[{"title": r.title, "emerged": r.emerged} for r in timeline.ladder],
            seniority=timeline.seniority,
            resolved_by_model=response.model,
        )
    )
    db.flush()
    return timeline
