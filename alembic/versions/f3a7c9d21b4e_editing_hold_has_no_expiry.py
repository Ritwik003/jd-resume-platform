"""EDITING hold has no expiry — only an approved (RESERVED) hold carries a clock

Editing is somebody's open loop, not a lock that should punish a closed tab.
Only the approved-and-waiting-on-the-vendor phase (RESERVED) keeps a clock now,
so ``profile_reservations.expires_at`` has to allow NULL for the open-ended
EDITING case.

Revision ID: f3a7c9d21b4e
Revises: d5b81f0a4c92
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c9d21b4e"
down_revision: Union[str, None] = "d5b81f0a4c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("profile_reservations") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade() -> None:
    # An open EDITING hold has no expiry to restore; give it a fresh one so the
    # column can go back to NOT NULL rather than losing the row.
    op.execute(
        "UPDATE profile_reservations SET expires_at = reserved_at WHERE expires_at IS NULL"
    )
    with op.batch_alter_table("profile_reservations") as batch:
        batch.alter_column("expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
