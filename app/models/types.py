"""Portable column types.

The reference backend targets PostgreSQL exclusively. This project keeps the
same PostgreSQL behaviour in production while remaining runnable on SQLite for
local demos, so UUID and JSON columns are wrapped in dialect-aware decorators.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import CHAR, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.types import JSON as GenericJSON


class GUID(TypeDecorator):
    """UUID column: native ``uuid`` on PostgreSQL, 36-char string elsewhere."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONType(TypeDecorator):
    """JSON column: ``JSONB`` on PostgreSQL, TEXT-backed JSON elsewhere."""

    impl = GenericJSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(GenericJSON())


class StringList(TypeDecorator):
    """A list[str] persisted as a JSON array (aliases, keywords, responsibilities)."""

    impl = GenericJSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(GenericJSON())

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return json.loads(value)
        return list(value)

    def process_result_value(self, value: Any, dialect) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return json.loads(value)
        return list(value)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
