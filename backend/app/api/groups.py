"""Wardrobe group endpoints.

A group is a named subset of the user's wardrobe; suggestions can be limited to one so the AI
only combines clothes the user already grouped together (a commute capsule, gym kit, travel bag).
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.database import DbSession
from app.models.group import ItemGroup
from app.models.user import User
from app.schemas.group import (
    GroupCreate,
    GroupDetailResponse,
    GroupItemsUpdate,
    GroupResponse,
    GroupUpdate,
)
from app.services.group_service import (
    GroupItemsError,
    GroupLimitError,
    GroupNameTakenError,
    GroupNotFoundError,
    GroupService,
)
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/groups", tags=["Groups"])


def _summary(
    group: ItemGroup, item_count: int, preview_items: list
) -> GroupResponse:
    return GroupResponse(
        id=group.id,
        name=group.name,
        item_count=item_count,
        preview_items=preview_items,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


async def _detail(service: GroupService, group: ItemGroup) -> GroupDetailResponse:
    item_ids = await service.item_ids_for_group(group.id)
    previews = await service.previews([group.id])
    summary = _summary(group, len(item_ids), previews.get(group.id, []))
    return GroupDetailResponse(**summary.model_dump(), item_ids=item_ids)


@router.get("", response_model=list[GroupResponse])
async def list_groups(
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[GroupResponse]:
    service = GroupService(db)
    groups = await service.list_for_user(current_user.id)
    group_ids = [group.id for group in groups]
    counts = await service.counts(group_ids)
    previews = await service.previews(group_ids)
    return [
        _summary(group, counts.get(group.id, 0), previews.get(group.id, []))
        for group in groups
    ]


@router.post("", response_model=GroupDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupDetailResponse:
    service = GroupService(db)
    try:
        group = await service.create(current_user.id, payload.name)
    except GroupLimitError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from None
    except GroupNameTakenError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from None
    return await _detail(service, group)


@router.get("/{group_id}", response_model=GroupDetailResponse)
async def get_group(
    group_id: uuid.UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupDetailResponse:
    service = GroupService(db)
    try:
        group = await service.get_for_user(current_user.id, group_id)
    except GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from None
    return await _detail(service, group)


@router.patch("/{group_id}", response_model=GroupDetailResponse)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupDetailResponse:
    service = GroupService(db)
    try:
        group = await service.rename(current_user.id, group_id, payload.name)
    except GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from None
    except GroupNameTakenError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from None
    return await _detail(service, group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    service = GroupService(db)
    try:
        await service.delete(current_user.id, group_id)
    except GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from None


@router.put("/{group_id}/items", response_model=GroupDetailResponse)
async def set_group_items(
    group_id: uuid.UUID,
    payload: GroupItemsUpdate,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupDetailResponse:
    service = GroupService(db)
    try:
        await service.set_items(current_user.id, group_id, payload.item_ids)
        group = await service.get_for_user(current_user.id, group_id)
    except GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from None
    except GroupItemsError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from None
    return await _detail(service, group)
