"""Offset pagination helpers shared by every list endpoint."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query

DEFAULT_SKIP = 0
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(slots=True)
class Pagination:
    skip: int
    limit: int


def pagination_params(
    skip: int = Query(DEFAULT_SKIP, ge=0),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> Pagination:
    return Pagination(skip=skip, limit=limit)
