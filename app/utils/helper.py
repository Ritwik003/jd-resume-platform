"""Small shared helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """Normalise naive datetimes (SQLite round-trips) to UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def slugify(value: str, *, max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip()
    value = re.sub(r"[\s_-]+", "_", value)
    return value[:max_length] or "file"


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def format_month_year(value: date | None, fallback: str = "") -> str:
    return value.strftime("%b %Y") if value else fallback


def next_reference_code(prefix: str, count: int) -> str:
    return f"{prefix}-{count + 1:05d}"


def allocate_reference_code(db, column, prefix: str) -> str:
    """The next code after the HIGHEST one in use — never the row count.

    Deriving it from ``COUNT(*)`` looked equivalent and was not: delete one row
    and the counter walks backwards onto a code that already exists, and the
    unique index on it rejects every insert from then on. That is not
    hypothetical — deleting a single abandoned draft made it impossible to
    start any new session, with a 500 from a UniqueViolation on
    reference_code and nothing on screen to explain why.

    MAX() is safe to read as a string here because the suffix is zero-padded
    to a fixed width, so lexical and numeric order agree (RS-00010 > RS-00009).

    One caveat worth stating rather than hiding: two creates racing each other
    can still read the same MAX and collide. That is a much narrower window
    than the deletion bug this replaces — and it fails loudly on the unique
    index rather than corrupting anything — but it is not fixed here.
    """
    from sqlalchemy import func

    highest = db.query(func.max(column)).filter(column.like(f"{prefix}-%")).scalar()
    if not highest:
        return f"{prefix}-00001"
    try:
        used = int(str(highest).rsplit("-", 1)[-1])
    except ValueError:  # pragma: no cover — a hand-edited code we cannot parse
        used = db.query(column).filter(column.like(f"{prefix}-%")).count()
    return f"{prefix}-{used + 1:05d}"
