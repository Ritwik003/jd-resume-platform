"""Role timelines, resolved per role instead of hard-coded

The platform carried a hand-written table mapping job titles to the year they
appeared, plus a hand-written ladder of earlier titles per discipline. Both
were wrong for any role nobody had added, and neither could account for how a
particular advert words its own role ("AI Engineer (Azure)").

This table holds the answer derived for each role actually seen, so the cost
is one model call per distinct role rather than one per resume, and a role the
platform has never met is handled correctly the first time.

Revision ID: d5f9b3c71a08
Revises: c8e1a4b70d35
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import app.models.types
from alembic import op

revision: str = "d5f9b3c71a08"
down_revision: str | None = "c8e1a4b70d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "role_timelines",
        sa.Column("id", app.models.types.GUID(), primary_key=True),
        sa.Column("role_key", sa.String(length=255), nullable=False),
        sa.Column("role_text", sa.String(length=255), nullable=False),
        sa.Column("discipline", sa.String(length=255), nullable=False),
        sa.Column("emerged", sa.Integer(), nullable=True),
        sa.Column("ladder", app.models.types.JSONType(), nullable=True),
        sa.Column("seniority", app.models.types.JSONType(), nullable=True),
        sa.Column("resolved_by_model", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_role_timelines_role_key", "role_timelines", ["role_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_role_timelines_role_key", table_name="role_timelines")
    op.drop_table("role_timelines")
