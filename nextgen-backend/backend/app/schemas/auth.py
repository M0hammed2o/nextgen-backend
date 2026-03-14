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
    """Staff login with business_code + PIN."""
    business_code: str = Field(min_length=6, max_length=6, pattern=r"^[A-Z0-9]{6}$")
    pin: str = Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")


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
