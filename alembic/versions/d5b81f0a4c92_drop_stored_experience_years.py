"""Stop storing a years-of-experience total on the person or the profile

Every resume is composed for one JD out of the shared bank, so the experience a
resume asserts comes from the JD's own minimum and from the employment dates the
composition prints. A stored career total was a second, competing answer: it
gated resume import behind a number nobody could verify, and it drifted the
moment a project was added. What a person really did is on their employment and
project rows, with dates, which is where a span is derived when one is wanted.

Revision ID: d5b81f0a4c92
Revises: c7a2e50b19d4
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5b81f0a4c92"
down_revision: Union[str, None] = "c7a2e50b19d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch mode: SQLite cannot DROP COLUMN in place, and the platform runs on
    # SQLite in development and Postgres in production off the same migration.
    with op.batch_alter_table("person_profiles") as batch:
        batch.drop_column("relevant_experience_years")
    with op.batch_alter_table("persons") as batch:
        batch.drop_column("total_experience_years")


def downgrade() -> None:
    # The values are gone, not archived — a downgrade restores the columns
    # empty, which is the same state a fresh import would leave them in.
    with op.batch_alter_table("persons") as batch:
        batch.add_column(sa.Column("total_experience_years", sa.Float(), nullable=True))
    with op.batch_alter_table("person_profiles") as batch:
        batch.add_column(sa.Column("relevant_experience_years", sa.Float(), nullable=True))
