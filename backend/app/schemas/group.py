"""Request and response models for wardrobe groups."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

GROUP_NAME_MAX_LENGTH = 50


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Group name cannot be blank")
        return name


class GroupUpdate(GroupCreate):
    pass


class GroupItemsUpdate(BaseModel):
    item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class GroupItemPreview(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None = None
    type: str
    thumbnail_url: str | None = None


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    item_count: int
    preview_items: list[GroupItemPreview]
    created_at: datetime
    updated_at: datetime


class GroupDetailResponse(GroupResponse):
    item_ids: list[uuid.UUID]
