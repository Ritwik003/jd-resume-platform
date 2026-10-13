"""Append-only audit helper."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID | None = None,
    actor_id: UUID | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        summary=summary,
        payload=payload or {},
    )
    db.add(entry)
    return entry
