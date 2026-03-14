"""
Pagination utilities — standardized response envelopes.

Cursor-based for real-time data (orders, messages).
Offset-based for static data (menu items, audit logs).
"""

import base64
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ── Response Envelopes ───────────────────────────────────────────────────────

class PaginationMeta(BaseModel):
    total: int | None = None
    page: int | None = None
    per_page: int
    next_cursor: str | None = None
    has_more: bool = False


class PaginatedResponse(BaseModel):
    data: list
    pagination: PaginationMeta


# ── Offset Pagination Params ─────────────────────────────────────────────────

class OffsetParams(BaseModel):
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


# ── Cursor Pagination Params ─────────────────────────────────────────────────

class CursorParams(BaseModel):
    cursor: str | None = None
    limit: int = Field(default=25, ge=1, le=100)


def encode_cursor(created_at: datetime, record_id: uuid.UUID) -> str:
    """Encode a cursor from timestamp + ID for deterministic ordering."""
    raw = f"{created_at.isoformat()}|{record_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor back to (created_at, record_id)."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.split("|", 1)
    return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
