"""Section 16 — Company pool (reusable master data)."""

from __future__ import annotations

from sqlalchemy import Column, String

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Company(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "companies"

    name = Column(String(255), nullable=False, unique=True, index=True)
    #: The role this employer is offered under on a resume — matched against a
    #: JD's role the same way ``Project.role`` is, independent of any project.
    role = Column(String(255), nullable=True)
    #: Free-form extras ("other metadata" in section 16) without schema churn.
    metadata_json = Column(String(4000), nullable=True)

    def __repr__(self) -> str:
        return f"<Company {self.name}>"
