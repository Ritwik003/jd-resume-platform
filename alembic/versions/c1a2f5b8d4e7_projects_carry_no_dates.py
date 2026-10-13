"""Projects carry no dates at all

A project is a standalone capability record — name, role, description — and
never prints a timeline; only the experience section has dates. This drops
the per-resume date overrides that only ever applied to a project row.

* ``resume_selected_projects`` drops ``start_date_override``, ``end_date_override``.

Revision ID: c1a2f5b8d4e7
Revises: b7e4f1a9c3d6
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1a2f5b8d4e7"
down_revision: Union[str, None] = "b7e4f1a9c3d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("resume_selected_projects") as batch:
        batch.drop_column("start_date_override")
        batch.drop_column("end_date_override")


def downgrade() -> None:
    with op.batch_alter_table("resume_selected_projects") as batch:
        batch.add_column(sa.Column("start_date_override", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("end_date_override", sa.String(length=20), nullable=True))
