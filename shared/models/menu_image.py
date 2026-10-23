"""
Ordered, named menu-image pages (migration 0058_business_menu_images).

Deliberately a NEW, minimal table rather than overloading `businesses.
menu_image_url`. That column is a single string and stays completely
untouched by this feature — every existing single-image business keeps
working exactly as before, reading the same field through the same
pipeline.py code path. This table exists purely for businesses that want
MULTIPLE named, ordered pages (e.g. "Regular Menu — Page 1/2",
"Secret Creations") shown in the Manager Dashboard, with no WhatsApp
delivery behaviour implied or changed by its existence alone.

Each row points at an `Asset` row (business-scoped, re-verified on every
read — see routes_menu_images.py) rather than storing a raw URL, reusing
the exact same asset_id -> storage_path -> public_url resolution already
used for business logos (`routes_assets.py::resolve_business_logo_url`)
and menu item images, instead of inventing a second way to store image
locations.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BusinessMenuImage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "business_menu_images"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        comment="assets.id, kind=BUSINESS_MENU_IMAGE. Ownership (asset.business_id "
                "== this row's business_id) is re-checked on every read, not just "
                "assumed from this FK -- same defensive pattern as resolve_business_logo_url.",
    )
    name: Mapped[str] = mapped_column(
        String(120), nullable=False,
        comment='Owner-facing label, e.g. "Regular Menu — Page 1", "Secret Creations".',
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true",
        comment="Soft-remove flag so a page can be taken down without losing the "
                "Asset row or breaking anything that cached its old sort position.",
    )

    def __repr__(self) -> str:
        return f"<BusinessMenuImage business={self.business_id} name={self.name!r}>"
