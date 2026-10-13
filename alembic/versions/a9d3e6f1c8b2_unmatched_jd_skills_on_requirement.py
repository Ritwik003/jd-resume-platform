"""Unmatched JD skills stored on the requirement

Revision ID: a9d3e6f1c8b2
Revises: c1a2f5b8d4e7
Create Date: 2026-08-28 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9d3e6f1c8b2"
down_revision: Union[str, None] = "c1a2f5b8d4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unmatched_mandatory_skills", sa.Text(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("unmatched_preferred_skills", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.drop_column("unmatched_preferred_skills")
        batch_op.drop_column("unmatched_mandatory_skills")
