"""How much of a JD the shared bank can actually evidence.

A person record carries only an identity — name, phone, email. There is no
stored job title, no career total and no base resume, so there is nothing to
compare a JD *against* at the person level and nothing to pass through
untouched. Every version is composed for the JD from the shared pool, which
means moulding is the generation step rather than an optional extra.

What is still worth measuring is the bank: of the skills this JD demands, how
many can be evidenced from the projects and technologies on record at all? A
requirement the pool cannot answer is the one useful signal here, and it is what
drives the missing-skill warning.

The score is arithmetic, not a model call. It runs on every generation, it has
to be explainable to whoever reads the resume, and a model asked "how well does
this match, 0-100" returns a different number each time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume_session import ResumeCompositionSession
from app.repositories import (
    project_repository,
    requirement_repository,
    technology_repository,
)

logger = logging.getLogger(__name__)

#: Words that appear in most job titles and so carry no matching signal.
_TITLE_NOISE = {
    "senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate",
    "i", "ii", "iii", "iv", "specialist", "consultant", "expert", "professional",
}

#: Titles that mean the same job. Without these "ML Engineer" scores zero
#: against "Machine Learning Engineer", which would mould a resume that needs no
#: moulding at all.
_TITLE_SYNONYMS: dict[str, str] = {
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "genai": "generative artificial intelligence",
    "sde": "software development engineer",
    "swe": "software engineer",
    "devops": "development operations",
    "sre": "site reliability engineer",
    "qa": "quality assurance",
    "ba": "business analyst",
    "pm": "project manager",
    "fullstack": "full stack",
    "backend": "back end",
    "frontend": "front end",
    "dba": "database administrator",
    "nlp": "natural language processing",
    "cv": "computer vision",
}

#: Mandatory skills dominate: a JD's must-haves are what a submission is judged
#: on, and its nice-to-haves rarely decide anything.
WEIGHT_MANDATORY = 0.80
WEIGHT_PREFERRED = 0.20


@dataclass(slots=True)
class FitAssessment:
    """The score plus everything needed to explain it to a human."""

    score: int
    mould_required: bool
    role_score: int
    mandatory_score: int
    preferred_score: int
    matched_mandatory: list[str] = field(default_factory=list)
    missing_mandatory: list[str] = field(default_factory=list)
    matched_preferred: list[str] = field(default_factory=list)
    jd_role: str | None = None
    profile_role: str | None = None
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "mould_required": self.mould_required,
            "role_score": self.role_score,
            "mandatory_score": self.mandatory_score,
            "preferred_score": self.preferred_score,
            "matched_mandatory": self.matched_mandatory,
            "missing_mandatory": self.missing_mandatory,
            "matched_preferred": self.matched_preferred,
            "jd_role": self.jd_role,
            "profile_role": self.profile_role,
            "basis": self.basis,
        }


# ───────────────────────────────────────────────────────── title comparison
def _title_tokens(title: str | None) -> set[str]:
    if not title:
        return set()
    words = re.split(r"[^a-z0-9+#]+", title.lower())
    expanded: list[str] = []
    for word in words:
        if not word or word in _TITLE_NOISE:
            continue
        expanded.extend(_TITLE_SYNONYMS.get(word, word).split())
    return {w for w in expanded if w}


def role_similarity(jd_role: str | None, profile_role: str | None) -> int:
    """0-100 overlap between two job titles.

    Overlap is divided by the smaller token set rather than the union: "Data
    Scientist" against "Senior Data Scientist (NLP)" is the same job, and
    penalising the longer title for carrying more words would mould a resume
    that needs no moulding.
    """
    a, b = _title_tokens(jd_role), _title_tokens(profile_role)
    if not a or not b:
        return 0
    return round(100 * len(a & b) / min(len(a), len(b)))


# ──────────────────────────────────────────────────────── topical relevance
#: Words too common in JD prose and technology copy to carry any matching
#: signal on their own — the free-text counterpart to ``_TITLE_NOISE``.
_TEXT_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "as", "at", "by", "from", "that",
    "this", "will", "using", "use", "used", "experience", "years", "year",
    "strong", "good", "ability", "skills", "skill", "working", "work",
    "team", "teams", "including", "etc", "we", "you", "our", "their",
    "role", "roles", "job", "candidate", "candidates", "responsible",
    "across", "into", "such", "other", "various", "related", "new",
}


def _text_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.split(r"[^a-z0-9+#]+", text.lower())
    return {w for w in words if w and w not in _TEXT_STOPWORDS and len(w) > 1}


def jd_context_tokens(requirement: Requirement | None) -> set[str]:
    """Every word the JD itself supplies, for a topical (not exact-name) match.

    Feeds ``topical_overlap`` when ranking bank rows that carry a
    ``description``/``category`` but no title of their own to compare with
    ``role_similarity`` — a technology's category, or a project's short
    description, is judged against this rather than against the JD role alone.
    """
    if requirement is None:
        return set()
    parts = [requirement.parsed_role or "", requirement.domain or ""]
    parts.extend(requirement.responsibilities or [])
    parts.extend(requirement.other_requirements or [])
    tokens: set[str] = set()
    for part in parts:
        tokens |= _text_tokens(part)
    return tokens


def topical_overlap(jd_tokens: set[str], candidate_text: str | None) -> int:
    """0-100: how much of ``candidate_text`` speaks to the JD's own words.

    Divided by the candidate's own token count rather than the union: a short,
    on-topic description that is entirely covered by the JD's vocabulary
    should score as fully relevant, not be penalised for being short.
    """
    candidate = _text_tokens(candidate_text)
    if not jd_tokens or not candidate:
        return 0
    return round(100 * len(jd_tokens & candidate) / len(candidate))


# ─────────────────────────────────────────── how much a label discriminates
def label_discrimination(counts: dict[str, int], label: str | None, total: int) -> float:
    """0-1: how much knowing this row's role tells you about the row.

    A label almost every row shares is not evidence. In the live bank 27 of 37
    projects are filed as "Full Stack Developer", so for 73% of the pool the
    role term produced the same number and the ranking between them collapsed
    onto whatever was left — which is exactly how a specific, on-topic project
    ended up below a generic one that happened to share a common word.

    Weighting by rarity is the standard answer (it is the idea behind IDF) and
    it needs no new data, which matters here because the fields that WOULD
    carry domain — ``Project.keywords`` — are empty on every row in the bank.

    A label held by one row scores 1. A label held by every row scores 0. An
    unlabelled row scores 0 too: absence of a role is not evidence either.
    """
    if not label or total <= 1:
        return 0.0 if not label else 1.0
    shared = counts.get(_norm_label(label), 0)
    if shared <= 0:
        return 1.0
    return max(0.0, 1.0 - (shared - 1) / (total - 1))


def _norm_label(label: str | None) -> str:
    return " ".join((label or "").lower().split())


def label_counts(labels: list[str | None]) -> dict[str, int]:
    """How many rows share each role label, for ``label_discrimination``."""
    counts: dict[str, int] = {}
    for label in labels:
        key = _norm_label(label)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def domain_affinity(domain: str | None, jd_tokens: set[str], *texts: str | None) -> int:
    """0-100: how much this row's own prose speaks to the JD's subject matter.

    Reads every piece of text the row has rather than the one-line summary the
    ranking used before — a project's detailed description and its
    responsibilities are where the domain actually shows, and judging "is this
    a payments system or an HR one" from a single short line throws most of
    the evidence away.

    The JD's stated ``domain`` is weighted double when it is set, because it is
    the one field on the requirement that names the subject outright. It is
    frequently empty (no JD parsed so far has filled it), so it lifts the score
    when present and is simply absent otherwise rather than being required.
    """
    haystack = _text_tokens(" ".join(t for t in texts if t))
    if not haystack:
        return 0

    # Normalised by the JD's vocabulary, not the project's. Dividing by the
    # project's own token count asks "what fraction of this project is about
    # the JD", which punishes a thoroughly documented project for having more
    # words — a one-line stub scored higher than the detailed write-up of the
    # very same system. The question worth asking is the other one: how much of
    # what the JD talks about does this project cover.
    context = 0.0
    if jd_tokens:
        context = 100 * len(jd_tokens & haystack) / len(jd_tokens)

    domain_tokens = _text_tokens(domain)
    if not domain_tokens:
        return max(0, min(100, round(context)))

    # A stated domain is the strongest thing the requirement says about subject
    # matter, so it leads and the looser context match only tops it up.
    named = 100 * len(domain_tokens & haystack) / len(domain_tokens)
    return max(0, min(100, round(0.75 * named + 0.25 * context)))


# ─────────────────────────────────────────────────────────── skill coverage
def _bank_skill_names(db: Session) -> set[str]:
    """Every technology the bank can evidence, lower-cased.

    The pool is shared, so this is not "what does this person know" — it is
    "what can we put on a resume at all". A JD skill absent from here cannot be
    covered by any composition, which is exactly what the warning reports.
    """
    names = {
        t.name.strip().lower() for t in technology_repository.list_all(db) if t and t.name
    }
    project_ids = [p.id for p in db.query(Project).filter(Project.is_active.is_(True)).all()]
    return names | project_repository.technology_names_for_projects(db, project_ids)


def _coverage(wanted: list[str], have: set[str]) -> tuple[int, list[str], list[str]]:
    if not wanted:
        return 100, [], []
    matched: list[str] = []
    missing: list[str] = []
    for name in wanted:
        (matched if name.strip().lower() in have else missing).append(name)
    return round(100 * len(matched) / len(wanted)), matched, missing


# ────────────────────────────────────────────────────────────────── entry
def assess(db: Session, session: ResumeCompositionSession) -> FitAssessment:
    """Score this session's profile against its JD."""
    requirement = requirement_repository.get(db, session.requirement_id)
    # A person no longer carries a stored role, so there is no title to compare.
    profile_role = None
    jd_role = requirement.parsed_role if requirement else None

    if requirement is None:
        # Nothing to compose toward.
        return FitAssessment(
            score=0,
            mould_required=False,
            role_score=0,
            mandatory_score=0,
            preferred_score=0,
            basis="No requirement is attached to this session, so there is nothing to compose toward.",
        )

    have = _bank_skill_names(db)
    mandatory, preferred = requirement_repository.mandatory_and_preferred(db, requirement.id)

    mandatory_score, matched_m, missing_m = _coverage(mandatory, have)
    preferred_score, matched_p, _ = _coverage(preferred, have)
    role_score = 0

    # Only the skills carry signal now. There is no stored job title to compare
    # against, so the score is what the bank can evidence against what the JD
    # asks for — nothing else.
    score = max(0, min(100, round(0.8 * mandatory_score + 0.2 * preferred_score)))

    # Always. There is no stored base resume to pass through untouched: every
    # version is composed for this JD out of the shared pool, so moulding IS the
    # generation step rather than an optional extra applied to something older.
    mould_required = True

    basis = (
        f"Fit {score}% — the bank evidences {mandatory_score}% of what "
        f"'{jd_role or 'this JD'}' requires and {preferred_score}% of what it prefers. "
        f"The resume is composed for this role from the shared pool."
    )
    if missing_m:
        basis += f" Nothing in the bank evidences: {', '.join(sorted(missing_m))}."

    return FitAssessment(
        score=score,
        mould_required=mould_required,
        role_score=role_score,
        mandatory_score=mandatory_score,
        preferred_score=preferred_score,
        matched_mandatory=sorted(matched_m),
        missing_mandatory=sorted(missing_m),
        matched_preferred=sorted(matched_p),
        jd_role=jd_role,
        profile_role=profile_role,
        basis=basis,
    )
