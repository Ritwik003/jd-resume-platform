"""Projects and companies become independent, JD-role-matched pools

Neither carries a link to the other or to any person any more. A project is a
standalone capability record — name, role, description, technologies — matched
to a JD by role-similarity search, same as a company now is. Section 17's
"show this project under a different company" feature goes with it: there is
no longer a real company on a project to reattribute away from.

* ``projects`` drops ``person_id``, ``company_id``, ``client_or_industry``,
  ``team_size``, ``start_date``, ``end_date``, ``duration_label``,
  ``architecture``, ``achievements``.
* ``project_companies`` (the "additional company associations" table) is
  dropped outright.
* ``companies`` gains ``role``, matched against a JD's role the same way
  ``projects.role`` already is.
* ``resume_selected_projects`` drops ``display_company_id`` and
  ``display_company_reason`` — the per-resume reattribution override.

Revision ID: b7e4f1a9c3d6
Revises: f3a7c9d21b4e
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4f1a9c3d6"
down_revision: Union[str, None] = "f3a7c9d21b4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("role", sa.String(length=255), nullable=True))

    # batch mode: several of these carry FK constraints, and SQLite can only
    # drop a constrained column by recreating the table.
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("person_id")
        batch.drop_column("company_id")
        batch.drop_column("client_or_industry")
        batch.drop_column("team_size")
        batch.drop_column("start_date")
        batch.drop_column("end_date")
        batch.drop_column("duration_label")
        batch.drop_column("architecture")
        batch.drop_column("achievements")

    with op.batch_alter_table("resume_selected_projects") as batch:
        batch.drop_column("display_company_id")
        batch.drop_column("display_company_reason")

    op.drop_table("project_companies")


def downgrade() -> None:
    op.create_table(
        "project_companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=30), nullable=False, server_default="EMPLOYER"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "company_id", "relationship_type", name="uq_project_company_rel"),
    )

    with op.batch_alter_table("resume_selected_projects") as batch:
        batch.add_column(sa.Column("display_company_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("display_company_id", sa.Uuid(), nullable=True))

    # The dropped columns' data is gone, not archived — a downgrade restores
    # them empty. person_id is NOT NULL going forward, so existing rows need a
    # placeholder before the constraint can be restored; there is no honest
    # value to backfill it with, so this direction is left to fail loudly
    # rather than invent an owner for every project.
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("achievements", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("architecture", sa.Text(), nullable=True))
        batch.add_column(sa.Column("duration_label", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("end_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("start_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("team_size", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("client_or_industry", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("company_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("person_id", sa.Uuid(), nullable=True))

    op.drop_column("companies", "role")
