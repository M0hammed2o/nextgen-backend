"""
Auth schemas — request/response models for login, token refresh, etc.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ── Login Requests ───────────────────────────────────────────────────────────

class EmailLoginRequest(BaseModel):
    """Owner/Manager login with email + password."""
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class PinLoginRequest(BaseModel):
    """Staff login with business_code + staff identity + PIN."""
    business_code: str = Field(min_length=6, max_length=6, pattern=r"^[A-Z0-9]{6}$")
    staff_id: uuid.UUID = Field(description="Selected staff member (from /auth/pin/staff)")
    pin: str = Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")


class StaffDirectoryRequest(BaseModel):
    """Look up the staff name list for the PIN login screen."""
    business_code: str = Field(min_length=6, max_length=6, pattern=r"^[A-Z0-9]{6}$")


class StaffDirectoryEntry(BaseModel):
    """One selectable staff member on the PIN login screen."""
    id: uuid.UUID
    staff_name: str


class StaffDirectoryResponse(BaseModel):
    business_name: str
    staff: list[StaffDirectoryEntry]


class SetPasswordRequest(BaseModel):
    """
    Complete a forced password reset (OWNER/MANAGER only). current_password
    is the temporary password the admin generated — re-verified here as the
    same trust boundary /auth/login already uses, rather than issuing a
    separate reset token. Confirming new_password twice is a client-side-only
    concern; the server only needs the single final value.
    """
    email: EmailStr
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    """Token refresh using refresh_token."""
    refresh_token: str


class LogoutRequest(BaseModel):
    """Logout — revoke refresh token."""
    refresh_token: str


# ── Token Responses ──────────────────────────────────────────────────────────

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="Access token TTL in seconds")


class TokenResponse(BaseModel):
    tokens: TokenPair
    user: "AuthUserInfo"


class AuthUserInfo(BaseModel):
    id: uuid.UUID
    email: str | None = None
    staff_name: str | None = None
    role: str
    business_id: uuid.UUID | None = None
    business_name: str | None = None


# ── Me Endpoint ──────────────────────────────────────────────────────────────

class MeResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    staff_name: str | None
    role: str
    business_id: uuid.UUID | None
    business_name: str | None
    last_login_at: datetime | None


# Forward ref resolution
TokenResponse.model_rebuild()
