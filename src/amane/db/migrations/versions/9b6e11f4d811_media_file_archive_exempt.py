"""add media file manual archive exemption

Revision ID: 9b6e11f4d811
Revises: 668e214b1a76
Create Date: 2026-09-14 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b6e11f4d811"
down_revision: str | None = "668e214b1a76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_files") as batch:
        batch.add_column(sa.Column("archive_exempt", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_media_files_archive_exempt", ["archive_exempt"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("media_files") as batch:
        batch.drop_index("ix_media_files_archive_exempt")
        batch.drop_column("archive_exempt")
