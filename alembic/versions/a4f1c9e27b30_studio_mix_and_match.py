"""Studio mix-and-match: composed projects, project timelines, JD fit score

Three changes, all in service of the composition step:

* ``resume_selected_projects.project_id`` becomes nullable, so a project built
  inside the studio (not picked from the bank) can sit in the same ordered list
  as the picked ones and be attached to any company.
* That row gains its own technologies and date overrides, so a composed project
  is fully described without writing anything back to the master pool.
* The session records the JD fit score that decided whether version 1 was
  moulded toward the role or transformed as-is.

Revision ID: a4f1c9e27b30
Revises: 90c1d501b1c4
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4f1c9e27b30"
down_revision: Union[str, None] = "90c1d501b1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "resume_selected_projects",
        "project_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.add_column(
        "resume_selected_projects",
        sa.Column("technologies_override", sa.Text(), nullable=True),
    )
    op.add_column(
        "resume_selected_projects",
        sa.Column("start_date_override", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "resume_selected_projects",
        sa.Column("end_date_override", sa.String(length=20), nullable=True),
    )

    op.add_column(
        "resume_composition_sessions",
        sa.Column("fit_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "resume_composition_sessions",
        sa.Column("fit_basis", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resume_composition_sessions", "fit_basis")
    op.drop_column("resume_composition_sessions", "fit_score")
    op.drop_column("resume_selected_projects", "end_date_override")
    op.drop_column("resume_selected_projects", "start_date_override")
    op.drop_column("resume_selected_projects", "technologies_override")
    # Rows with a null project_id are studio-composed and cannot be represented
    # once the column is mandatory again, so they are removed rather than left
    # to fail the ALTER.
    op.execute("DELETE FROM resume_selected_projects WHERE project_id IS NULL")
    op.alter_column(
        "resume_selected_projects",
        "project_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
