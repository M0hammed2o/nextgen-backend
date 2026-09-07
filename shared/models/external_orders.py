"""
External marketplace orders: daily report imports from Uber Eats, Mr D and
any generic delivery platform.

WHY THERE IS NO "ExternalOrder" TABLE
------------------------------------
An imported sale becomes a real ``Order`` with an external ``source``. That
is deliberate: it means one canonical product, one canonical recipe, one
stock ledger and one set of reports across every channel. A parallel
external-order table would be a second inventory system that could disagree
with the first, which is exactly the failure this design avoids.

What lives here is only what the CANONICAL model has no place for:

* ``ExternalImportBatch``   - the audit record of one uploaded file
* ``ExternalProductMapping`` - how a platform's product/modifier names map
                               onto this tenant's menu, remembered so the
                               next day's file maps itself

Duplicate protection lives on ``orders`` (a partial unique index over
business + source + external_order_id), not here, because the thing that
must never happen twice is the ORDER, not the row that described it.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExternalImportBatch(Base, UUIDPrimaryKeyMixin):
    """
    One uploaded daily report, and what happened to it.

    Kept even when an import is blocked or fails, so "why is stock wrong?"
    can always be answered with what was uploaded, when, by whom, and what
    the system did with it.
    """

    __tablename__ = "external_import_batches"
    __table_args__ = (
        Index("ix_external_import_batches_business_platform", "business_id", "platform"),
        Index("ix_external_import_batches_business_time", "business_id", "uploaded_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="UBER_EATS / MR_D / GENERIC"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="PREVIEW",
        nullable=False,
        comment="PREVIEW / COMPLETED / BLOCKED / FAILED",
    )

    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # The reporting window the FILE covers, taken from the order timestamps
    # inside it - not the upload time. This is what tells a manager whether
    # today's sales are actually accounted for.
    period_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    movements_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    gross_sales_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Row-level problems, kept so a manager can see what was rejected without
    # re-uploading. Bounded to a sane number of entries by the service.
    issues_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<ExternalImportBatch {self.platform} [{self.status}]>"


class ExternalProductMapping(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    "Large Margherita Pizza" on Uber -> Margherita Pizza, size Large, here.

    Saved per tenant per platform so tomorrow's file maps itself. A mapping
    never creates a product: it only points an external string at something
    that already exists on this restaurant's menu, which is what keeps ONE
    canonical recipe across every channel.

    ``external_key`` is the normalised lookup form of ``external_text``
    (lower-cased, whitespace collapsed) so trivial formatting differences in
    the export do not produce a second unresolved item every day.
    """

    __tablename__ = "external_product_mappings"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "platform",
            "kind",
            "external_key",
            name="uq_external_product_mappings_business_platform_kind_key",
        ),
        Index("ix_external_product_mappings_business_platform", "business_id", "platform"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), default="ITEM", nullable=False, comment="ITEM / MODIFIER"
    )

    external_text: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Exactly what the export said"
    )
    external_key: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Normalised form used for lookup"
    )

    # -- Where it points (all nullable; which ones apply depends on kind) ----
    menu_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=True
    )
    option_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Size/option id within the item's options_json"
    )
    menu_add_on_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_add_ons.id", ondelete="CASCADE"), nullable=True
    )
    removable_ref_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="options_json.removable_ingredients[].id"
    )

    is_ignored: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Explicitly marked as not stock-affecting (e.g. a delivery "
        "fee line). Deliberate, recorded, and visible - never a silent skip.",
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    times_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ExternalProductMapping {self.platform}:{self.external_text!r}>"
