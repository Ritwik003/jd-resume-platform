"""Persons drop location

Never read by resume composition — a person is an identity only (see
app/models/person.py's own docstring); the resume's own contact location
(``ContactBlock.location``) is a separate, operator-editable field and is
untouched by this migration.

Revision ID: b3d5f8a17c94
Revises: f1c6a3e08d72
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d5f8a17c94"
down_revision: Union[str, None] = "f1c6a3e08d72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("persons") as batch:
        batch.drop_column("location")


def downgrade() -> None:
    # The dropped column's data is gone, not archived — a downgrade restores
    # it empty.
    with op.batch_alter_table("persons") as batch:
        batch.add_column(sa.Column("location", sa.String(length=255), nullable=True))
