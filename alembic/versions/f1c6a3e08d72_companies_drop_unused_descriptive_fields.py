"""Companies drop unused descriptive fields

``industry``, ``location``, ``website`` and ``description`` never fed the
resume: role-similarity is the only signal ``resume_composer`` ranks a
company on, and ``name``/``role`` are the only two fields a resume actually
prints for an employer. Carrying the rest just meant a form nobody filled in
and columns nobody read.

Revision ID: f1c6a3e08d72
Revises: e7b2c4d90a51
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c6a3e08d72"
down_revision: Union[str, None] = "e7b2c4d90a51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.drop_index(batch.f("ix_companies_industry"))
        batch.drop_column("industry")
        batch.drop_column("location")
        batch.drop_column("website")
        batch.drop_column("description")


def downgrade() -> None:
    # The dropped columns' data is gone, not archived — a downgrade restores
    # the columns empty.
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(sa.Column("website", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("location", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("industry", sa.String(length=150), nullable=True))
        batch.create_index(batch.f("ix_companies_industry"), ["industry"], unique=False)
