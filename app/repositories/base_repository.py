"""Generic CRUD helpers for the simple master-data tables."""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from app.config.database import Base
from app.utils.exceptions import ConflictError, NotFoundError

ModelT = TypeVar("ModelT", bound=Base)


def get_or_404(db: Session, model: type[ModelT], entity_id: UUID, label: str | None = None) -> ModelT:
    instance = db.query(model).filter(model.id == entity_id).first()
    if instance is None:
        raise NotFoundError(f"{label or model.__name__} not found")
    return instance


def ensure_unique(db: Session, model: type[ModelT], field: str, value: Any, *, exclude_id: UUID | None = None) -> None:
    query = db.query(model).filter(getattr(model, field) == value)
    if exclude_id is not None:
        query = query.filter(model.id != exclude_id)
    if query.first() is not None:
        raise ConflictError(f"{model.__name__} with {field}='{value}' already exists")


def list_and_count(query, skip: int, limit: int) -> tuple[list[Any], int]:
    total = query.count()
    return query.offset(skip).limit(limit).all(), total


def apply_updates(instance: ModelT, payload: Any, *, exclude: set[str] | None = None) -> ModelT:
    data = payload.model_dump(exclude_unset=True)
    for key in exclude or set():
        data.pop(key, None)
    for key, value in data.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    return instance
