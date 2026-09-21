"""
Admin users — separate table from business users.
Only Super Admins live here. Different security domain, no business scoping.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AdminRole:
    """
    Admin-plane role vocabulary — kept in ONE place so the DB CHECK
    constraint (see shared/migrations/versions/0034_onboarding_role.py),
    Pydantic schemas, and every Python role-gating check reference the same
    values rather than re-typed literals.

    SUPER_ADMIN: unrestricted platform access (existing role, unchanged).
    ONBOARDER: a sales partner who can create a restaurant tenant and hand
    it a working login, scoped to businesses they personally created and
    only until that business's onboarding is marked complete — see
    admin_api/app/core/deps.py::get_business_scoped. This is a real
    architectural decision (a new admin-plane role) — see the task's DOC
    NOTE for DECISIONS.md rather than assuming it's already logged there.
    """
    SUPER_ADMIN = "SUPER_ADMIN"
    ONBOARDER = "ONBOARDER"


ADMIN_ROLES = (AdminRole.SUPER_ADMIN, AdminRole.ONBOARDER)


class AdminUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), default="SUPER_ADMIN", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Security ─────────────────────────────────────────────────────────
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<AdminUser {self.email}>"
