"""Shared response envelopes."""

from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict

T = TypeVar("T")


def _coerce_bool(value: Any) -> bool:
    """Read a boolean that is stored as nullable text.

    Several columns keep flags as VARCHAR(10) holding 'true'/'false', and leave
    NULL for rows nothing ever set. A plain ``bool`` field cannot read those: a
    Pydantic default only fills an ABSENT key, while an ORM attribute is present
    and None — which fails validation and 500s the whole response. Treating
    "unset" as False is both correct and what every caller already assumed.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y", "t")


#: A boolean tolerant of NULL and of 'true'/'false' text.
FlexBool = Annotated[bool, BeforeValidator(_coerce_bool)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageMeta(BaseModel):
    skip: int
    limit: int
    total: int


class Page(BaseModel, Generic[T]):
    items: list[T]
    meta: PageMeta


class MessageResponse(BaseModel):
    message: str
