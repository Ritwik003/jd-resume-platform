"""Profile types drop description

A profile type is a role NAME and nothing else. Nothing in composition,
selection, ranking or validation ever read the column -- it was written by an
operator, stored, shown back on the Knowledge base screen, and never reached a
resume. Keeping it asked for prose that had no consumer.

The role vocabulary that DOES matter is kept elsewhere and is derived rather
than typed: app/services/roles.py holds the vocabulary the job descriptions
themselves supply, and role_timelines carries what a title was called in an
earlier era.

Revision ID: e2b7d4c95f31
Revises: d5f9b3c71a08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7d4c95f31"
down_revision: Union[str, None] = "d5f9b3c71a08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("profile_types") as batch:
        batch.drop_column("description")


def downgrade() -> None:
    with op.batch_alter_table("profile_types") as batch:
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
