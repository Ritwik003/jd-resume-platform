"""Reads for the shared company pool (section 16).

Companies are shared across people by name — that reuse is what makes section
17's mix-and-match possible at all, since two histories can only be recombined
if they point at the same company row.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.company import Company


def get(db: Session, company_id: UUID | None) -> Company | None:
    if company_id is None:
        return None
    return db.query(Company).filter(Company.id == company_id).first()


def list_all(db: Session, limit: int | None = None) -> list[Company]:
    query = db.query(Company).order_by(Company.name)
    return query.limit(limit).all() if limit else query.all()


def names(db: Session, limit: int = 200) -> list[str]:
    return [c.name for c in list_all(db, limit=limit)]


def index_by_name(db: Session) -> dict[str, Company]:
    """Lower-cased name -> Company, so an import reuses rather than duplicates."""
    return {c.name.strip().lower(): c for c in db.query(Company).all() if c.name}
