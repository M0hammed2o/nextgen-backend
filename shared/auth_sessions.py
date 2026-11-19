"""
Ending a business user's sessions (BUG-0184 / BUG-0206 follow-up).

One helper, used everywhere a credential is replaced or an account is
deactivated: staff PIN rotation, staff deactivation, password change, admin
password reset, onboarding-account retirement. It does two things in the
caller's transaction:

1. Bumps ``BusinessUser.token_version``. Every access token carries the
   version it was issued under (``tv`` claim), and the per-request check in
   ``backend/app/core/rbac.py::get_current_user`` rejects a mismatch, so an
   access token issued before the change stops working on its next request
   instead of running to its expiry.
2. Revokes every live refresh token, so the session cannot be renewed.

The caller commits. A token issued *after* the bump (for example the new
pair ``set_password`` returns) carries the new version and stays valid.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.user import BusinessUser, RefreshToken


async def end_user_sessions(db: AsyncSession, user: BusinessUser) -> None:
    user.token_version = (user.token_version or 0) + 1
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
