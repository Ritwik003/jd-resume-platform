"""Drop every stored date from the bank

Nothing about a person, a company or a project is dated any more. A resume's
timeline is not a record being reported: it is composed for one JD out of the
years that JD asks for, laid out at generation time. Keeping a second set of
dates in the bank only gave the composed timeline something to contradict.

Technology.first_release_date deliberately SURVIVES. It is not a date about
anybody's history — it is a fact about the world — and it is what the
anachronism checks compare the composed timeline against.

Revision ID: e7b2c4d90a51
Revises: d4c8b2e6f019
Create Date: 2026-08-31 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2c4d90a51"
down_revision: Union[str, None] = "d4c8b2e6f019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("employments", schema=None) as batch_op:
        batch_op.drop_column("start_date")
        batch_op.drop_column("end_date")
        batch_op.drop_column("is_current")

    with op.batch_alter_table("person_technologies", schema=None) as batch_op:
        batch_op.drop_column("first_used_date")


def downgrade() -> None:
    with op.batch_alter_table("person_technologies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("first_used_date", sa.Date(), nullable=True))

    with op.batch_alter_table("employments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_current", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("end_date", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("start_date", sa.String(length=10), nullable=True))
