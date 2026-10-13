"""Project-to-company pairing on the resume selection row

Revision ID: d4c8b2e6f019
Revises: a9d3e6f1c8b2
Create Date: 2026-08-31 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4c8b2e6f019"
down_revision: Union[str, None] = "a9d3e6f1c8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("resume_selected_projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_resume_selected_projects_company_id",
            "companies",
            ["company_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("resume_selected_projects", schema=None) as batch_op:
        batch_op.drop_constraint("fk_resume_selected_projects_company_id", type_="foreignkey")
        batch_op.drop_column("company_id")
