"""When a job title became a job — resolved per role, kept once.

The platform used to carry a hand-written table of titles and the years they
appeared. It was wrong the moment a JD arrived for a role nobody had added,
and it could not know how a particular advert words its own role. This table
replaces it: the answer is derived for the role in front of us and stored, so
the cost is one model call per distinct role ever seen, not one per resume.

Keyed on the NORMALISED role, so "AI Engineer (Azure)", "ai engineer (azure)"
and "AI Engineer(Azure)" resolve to the same row.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String

from app.config.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import JSONType


class RoleTimeline(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "role_timelines"

    #: The lookup key: the advert's role, lower-cased and squeezed.
    role_key = Column(String(255), nullable=False, unique=True, index=True)
    #: The role exactly as the advert wrote it, kept for the audit trail.
    role_text = Column(String(255), nullable=False)

    #: The job without the advert's stack qualifier or seniority word.
    discipline = Column(String(255), nullable=False)
    #: The year that title became a common job title.
    emerged = Column(Integer, nullable=True)
    #: [{"title": ..., "emerged": ...}], oldest first, ending at the discipline.
    ladder = Column(JSONType(), nullable=True)
    #: ["{role}", "Senior {role}", ...], junior first.
    seniority = Column(JSONType(), nullable=True)

    #: Which model answered, so a bad batch can be found and re-resolved.
    resolved_by_model = Column(String(120), nullable=True)

    def __repr__(self) -> str:
        return f"<RoleTimeline {self.role_key} emerged={self.emerged}>"
