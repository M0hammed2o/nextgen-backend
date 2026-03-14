"""
Assets — centralized file/image management.
Supports menu item images, business logos, special images.
Frontend uploads via presigned URLs; DB stores metadata.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class Asset(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "assets"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="MENU_ITEM_IMAGE / BUSINESS_LOGO / SPECIAL_IMAGE"
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="ID of the entity this asset belongs to (menu_item, business, special)"
    )
    storage_path: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Path in Supabase Storage bucket"
    )
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
