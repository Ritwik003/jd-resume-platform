"""Shared ORM mixins."""

from __future__ import annotations

from sqlalchemy import Column, DateTime
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql import func

from app.models.types import GUID, new_uuid


class UUIDPrimaryKeyMixin:
    @declared_attr
    def id(cls):  # noqa: N805
        return Column(GUID(), primary_key=True, default=new_uuid)


class TimestampMixin:
    @declared_attr
    def created_at(cls):  # noqa: N805
        return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @declared_attr
    def updated_at(cls):  # noqa: N805
        return Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )


class SoftDeleteMixin:
    @declared_attr
    def is_active(cls):  # noqa: N805
        from sqlalchemy import Boolean

        return Column(Boolean, nullable=False, default=True, server_default="1")
