"""
Temporary events (souks, fairs) and the vendors trading at them
(migration 0062_events_foundation, PRODUCT_ROADMAP.md §13 Wave 1).

A vendor is an ordinary ``Business`` (DECISION-0062 option A), so POS,
inventory, orders and tenant isolation work unchanged. These tables only add
what an event needs on top:

- ``events`` and ``event_days``: the event and its calendar. Each date is an
  explicit row with a billable flag, because a real event calendar need not
  be contiguous and the dates arrive separately from the duration.
- ``event_vendors``: which business trades at which event, in which category
  and at which stall. One business can trade at many events over time.
- ``event_vendor_subscriptions``: what a vendor bought. The day rate and
  billable-day count are frozen at purchase, so changing an event's rate
  never rewrites an existing invoice.

Admin-plane data: ``events`` has no ``business_id``. Event-vendor rows carry
``business_id`` so a vendor's own (business-plane) reads stay JWT-scoped.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Event(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "events"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    venue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Africa/Johannesburg")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    # VAT-inclusive prices per billable day, in cents (R100 food, R50 clothing).
    food_day_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    clothing_day_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Days before the first billable day during which a vendor may configure (migration 0063).
    prep_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14, server_default="14")
    # Vendor self-registration (migration 0064). Only a bcrypt hash of the code is stored.
    registration_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    registration_code_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    days = relationship("EventDay", back_populates="event", cascade="all, delete-orphan", order_by="EventDay.day")
    vendors = relationship("EventVendor", back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','OPEN','CLOSED','CANCELLED')", name="status_known"),
        CheckConstraint("food_day_rate_cents >= 0 AND clothing_day_rate_cents >= 0", name="rates_non_negative"),
        CheckConstraint("prep_days >= 0 AND prep_days <= 90", name="prep_days_range"),
    )


class EventDay(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "event_days"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    event = relationship("Event", back_populates="days")

    __table_args__ = (UniqueConstraint("event_id", "day", name="uq_event_days_event_id_day"),)


class EventVendor(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "event_vendors"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    stall_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event = relationship("Event", back_populates="vendors")
    subscriptions = relationship(
        "EventVendorSubscription", back_populates="vendor", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("event_id", "business_id", name="uq_event_vendors_event_id_business_id"),
        Index(
            "uq_event_vendors_event_id_stall_code",
            "event_id", "stall_code",
            unique=True,
            postgresql_where=text("stall_code IS NOT NULL"),
        ),
        Index("ix_event_vendors_business_id", "business_id"),
        CheckConstraint("category IN ('FOOD','CLOTHING')", name="category_known"),
        CheckConstraint(
            "status IN ('PENDING','ACTIVE','UNPAID','EXPIRED','CANCELLED')", name="status_known"
        ),
    )


class EventVendorSubscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "event_vendor_subscriptions"

    event_vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_vendors.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from event_vendors so a vendor's own reads filter on the JWT business_id directly.
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    day_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_days: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # VAT-inclusive
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNPAID")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # What the vendor says it paid with (migration 0064). Never marks anything paid.
    payment_claim_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    vendor = relationship("EventVendor", back_populates="subscriptions")

    __table_args__ = (
        Index("ix_event_vendor_subscriptions_event_vendor_id", "event_vendor_id"),
        Index("ix_event_vendor_subscriptions_business_id", "business_id"),
        # One payment reference settles at most one PAID subscription (migration 0063).
        Index(
            "uq_event_vendor_subscriptions_payment_reference_paid",
            "payment_reference",
            unique=True,
            postgresql_where=text("status = 'PAID'"),
        ),
        CheckConstraint("ends_on >= starts_on", name="range_ordered"),
        CheckConstraint("day_rate_cents >= 0 AND billable_days >= 0", name="non_negative"),
        CheckConstraint("amount_cents = day_rate_cents * billable_days", name="amount_matches"),
        CheckConstraint("status IN ('UNPAID','PAID','VOID')", name="status_known"),
        CheckConstraint("(status = 'PAID') = (paid_at IS NOT NULL)", name="paid_at_iff_paid"),
    )
