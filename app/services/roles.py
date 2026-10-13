"""Section 10 — the role vocabulary, kept by the job descriptions themselves.

A role ("GenAI Engineer", "Senior Data Scientist") is not something anybody sits
down to maintain. It arrives in a JD, and the only question worth asking is
whether we have seen it before. So parsing a JD registers its role here: a name
we already hold is reused, a new one is added, and the profile-type list becomes
a record of every role we have ever been asked for rather than a list somebody
has to remember to curate.

The same rule extends to people. A person is an identity and nothing more, so
"Amit, offered as a GenAI Engineer" is not a fact about Amit that anyone should
have to type — it is what selecting him for a GenAI Engineer JD *means*. That
row is therefore created at selection time and reused ever after, which is what
lets a profile type answer "who has gone out under this role, and to whom".

Matching is case- and space-insensitive so "GenAI engineer" and "GenAI Engineer"
do not become two roles; the first spelling seen is the one kept.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.person import PersonProfile, ProfileType

logger = logging.getLogger(__name__)


def _normalise(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def register_role(db: Session, name: str | None) -> ProfileType | None:
    """Return the profile type for ``name``, creating it the first time it appears.

    Returns ``None`` for a JD whose role could not be parsed — there is nothing
    to log, and inventing a placeholder role would put a row in the pool that no
    job description ever asked for.
    """
    cleaned = _normalise(name or "")
    if not cleaned:
        return None

    existing = (
        db.query(ProfileType)
        .filter(func.lower(ProfileType.name) == cleaned.lower())
        .first()
    )
    if existing is not None:
        # Seen before. Reactivate a role somebody had retired rather than
        # failing the unique constraint with a second row of the same name.
        if not existing.is_active:
            existing.is_active = True
        return existing

    profile_type = ProfileType(name=cleaned)
    db.add(profile_type)
    db.flush()
    logger.info("Registered new role from a JD: %s", cleaned)
    return profile_type


def profile_for_role(
    db: Session, *, person_id, profile_type: ProfileType, title: str | None = None
) -> PersonProfile:
    """The (person, role) row, created on first use.

    ``title`` is what prints under the candidate's name. It defaults to the role
    itself, which is what the JD asked for and therefore the right thing to lead
    with; the studio can still override it per resume.
    """
    existing = (
        db.query(PersonProfile)
        .filter(
            PersonProfile.person_id == person_id,
            PersonProfile.profile_type_id == profile_type.id,
        )
        .first()
    )
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
        return existing

    profile = PersonProfile(
        person_id=person_id,
        profile_type_id=profile_type.id,
        title=_normalise(title or "") or profile_type.name,
    )
    db.add(profile)
    db.flush()
    return profile
