"""add item groups

Revision ID: c7d8e9f0a1b2
Revises: f3b4c5d6e7a8
Create Date: 2026-11-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "a7d3c9e1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "item_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_item_groups_user_name"),
    )
    op.create_index("ix_item_groups_user_id", "item_groups", ["user_id"])

    op.create_table(
        "item_group_members",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["item_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["clothing_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "item_id"),
    )
    op.create_index("ix_item_group_members_item_id", "item_group_members", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_item_group_members_item_id", table_name="item_group_members")
    op.drop_table("item_group_members")
    op.drop_index("ix_item_groups_user_id", table_name="item_groups")
    op.drop_table("item_groups")
