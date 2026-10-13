"""Templates carry a blueprint and a Jinja source

An uploaded template is analysed once, at upload, into two artefacts:

* ``blueprint`` — the sections the design actually has, in its own order and
  its own wording. The content window is built from this, so editing a resume
  edits the design it will be printed into rather than a fixed set of fields.
* ``jinja_source`` — the same .tex with its worked example replaced by Jinja
  loops over the resume's data, so every later render is deterministic
  instead of a model writing LaTeX from scratch per resume.

``project_dir`` / ``main_tex_name`` hold a multi-file upload — an Overleaf
project is rarely one file, and a .cls sitting beside the .tex has to be
compiled beside it too.

Revision ID: c8e1a4b70d35
Revises: b3d5f8a17c94
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.models.types

revision: str = "c8e1a4b70d35"
down_revision: Union[str, None] = "b3d5f8a17c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("resume_templates") as batch:
        batch.add_column(sa.Column("blueprint", app.models.types.JSONType(), nullable=True))
        batch.add_column(sa.Column("jinja_source", sa.Text(), nullable=True))
        batch.add_column(sa.Column("project_dir", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("main_tex_name", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("resume_templates") as batch:
        batch.drop_column("main_tex_name")
        batch.drop_column("project_dir")
        batch.drop_column("jinja_source")
        batch.drop_column("blueprint")
