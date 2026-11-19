"""User-defined groups of wardrobe items.

A group is a named subset of one user's wardrobe. It exists so a recommendation can be limited
to a set of clothes the user picked once (a commute capsule, gym kit, travel bag) instead of
re-picking items on every generation.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.item import ClothingItem
    from app.models.user import User


class ItemGroup(Base):
    __tablename__ = "item_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_item_groups_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship("User", back_populates="item_groups")
    members: Mapped[list["ItemGroupMember"]] = relationship(
        "ItemGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class ItemGroupMember(Base):
    __tablename__ = "item_group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("item_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clothing_items.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["ItemGroup"] = relationship("ItemGroup", back_populates="members")
    item: Mapped["ClothingItem"] = relationship("ClothingItem")
