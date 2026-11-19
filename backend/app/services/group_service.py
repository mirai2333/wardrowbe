"""Service for user-defined wardrobe groups."""

import logging
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import ItemGroup, ItemGroupMember
from app.models.item import ClothingItem
from app.schemas.group import GroupItemPreview
from app.utils.signed_urls import sign_optional

logger = logging.getLogger(__name__)

MAX_GROUPS_PER_USER = 50
MAX_ITEMS_PER_GROUP = 500


class GroupNotFoundError(Exception):
    pass


class GroupNameTakenError(Exception):
    pass


class GroupLimitError(Exception):
    pass


class GroupItemsError(Exception):
    pass


class GroupService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_user(self, user_id: uuid.UUID) -> list[ItemGroup]:
        result = await self.db.execute(
            select(ItemGroup)
            .where(ItemGroup.user_id == user_id)
            .order_by(ItemGroup.name.asc())
        )
        return list(result.scalars().all())

    async def get_for_user(self, user_id: uuid.UUID, group_id: uuid.UUID) -> ItemGroup:
        result = await self.db.execute(
            select(ItemGroup).where(
                ItemGroup.id == group_id,
                ItemGroup.user_id == user_id,
            )
        )
        group = result.scalar_one_or_none()
        if group is None:
            raise GroupNotFoundError("Group not found")
        return group

    async def create(self, user_id: uuid.UUID, name: str) -> ItemGroup:
        count = await self.db.scalar(
            select(func.count()).select_from(ItemGroup).where(ItemGroup.user_id == user_id)
        )
        if (count or 0) >= MAX_GROUPS_PER_USER:
            raise GroupLimitError(f"You can create at most {MAX_GROUPS_PER_USER} groups")

        group = ItemGroup(user_id=user_id, name=name)
        self.db.add(group)
        await self._commit_or_name_taken()
        await self.db.refresh(group)
        return group

    async def rename(self, user_id: uuid.UUID, group_id: uuid.UUID, name: str) -> ItemGroup:
        group = await self.get_for_user(user_id, group_id)
        group.name = name
        await self._commit_or_name_taken()
        await self.db.refresh(group)
        return group

    async def delete(self, user_id: uuid.UUID, group_id: uuid.UUID) -> None:
        group = await self.get_for_user(user_id, group_id)
        await self.db.delete(group)
        await self.db.commit()

    async def set_items(
        self, user_id: uuid.UUID, group_id: uuid.UUID, item_ids: list[uuid.UUID]
    ) -> None:
        await self.get_for_user(user_id, group_id)

        unique_ids = list(dict.fromkeys(item_ids))
        if len(unique_ids) > MAX_ITEMS_PER_GROUP:
            raise GroupItemsError(f"A group cannot hold more than {MAX_ITEMS_PER_GROUP} items")

        if unique_ids:
            result = await self.db.execute(
                select(ClothingItem.id).where(
                    ClothingItem.user_id == user_id,
                    ClothingItem.id.in_(unique_ids),
                )
            )
            owned = set(result.scalars().all())
            missing = [str(item_id) for item_id in unique_ids if item_id not in owned]
            if missing:
                raise GroupItemsError(
                    "These items do not belong to you or no longer exist: " + ", ".join(missing)
                )

        await self.db.execute(
            delete(ItemGroupMember).where(ItemGroupMember.group_id == group_id)
        )
        self.db.add_all(
            [ItemGroupMember(group_id=group_id, item_id=item_id) for item_id in unique_ids]
        )
        await self.db.execute(
            update(ItemGroup)
            .where(ItemGroup.id == group_id)
            .values(updated_at=func.now())
        )
        await self.db.commit()

    async def item_ids_for_group(self, group_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self.db.execute(
            select(ItemGroupMember.item_id)
            .where(ItemGroupMember.group_id == group_id)
            .order_by(ItemGroupMember.added_at.asc())
        )
        return list(result.scalars().all())

    async def eligible_item_ids(self, user_id: uuid.UUID, group_id: uuid.UUID) -> list[uuid.UUID]:
        """Item ids a recommendation may draw from, after checking the group is the user's."""
        await self.get_for_user(user_id, group_id)
        return await self.item_ids_for_group(group_id)

    async def counts(self, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not group_ids:
            return {}
        result = await self.db.execute(
            select(ItemGroupMember.group_id, func.count())
            .where(ItemGroupMember.group_id.in_(group_ids))
            .group_by(ItemGroupMember.group_id)
        )
        return {group_id: count for group_id, count in result.all()}

    async def previews(
        self, group_ids: list[uuid.UUID], per_group: int = 4
    ) -> dict[uuid.UUID, list[GroupItemPreview]]:
        previews: dict[uuid.UUID, list[GroupItemPreview]] = {gid: [] for gid in group_ids}
        if not group_ids:
            return previews

        result = await self.db.execute(
            select(ItemGroupMember.group_id, ClothingItem)
            .join(ClothingItem, ClothingItem.id == ItemGroupMember.item_id)
            .where(ItemGroupMember.group_id.in_(group_ids))
            .order_by(ItemGroupMember.added_at.desc())
        )
        for group_id, item in result.all():
            bucket = previews[group_id]
            if len(bucket) >= per_group:
                continue
            bucket.append(
                GroupItemPreview(
                    id=item.id,
                    name=item.name,
                    type=item.type,
                    thumbnail_url=sign_optional(item.thumbnail_path),
                )
            )
        return previews

    async def _commit_or_name_taken(self) -> None:
        try:
            await self.db.commit()
        except IntegrityError as e:
            await self.db.rollback()
            logger.info(f"Group name conflict: {e}")
            raise GroupNameTakenError("A group with that name already exists") from None
