"""Templates uploaded as PDF or Word carry a derived LaTeX source

A PDF has no reflowable structure, so it cannot be filled. It is converted to a
self-contained .tex once at upload; that conversion lives in ``derived_latex``
while ``source_storage_path`` keeps pointing at the file the operator uploaded.

Revision ID: c7a2e50b19d4
Revises: a4f1c9e27b30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e50b19d4"
down_revision: Union[str, None] = "a4f1c9e27b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("resume_templates", sa.Column("derived_latex", sa.Text(), nullable=True))
    op.add_column(
        "resume_templates",
        sa.Column("derived_by_model", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resume_templates", "derived_by_model")
    op.drop_column("resume_templates", "derived_latex")
