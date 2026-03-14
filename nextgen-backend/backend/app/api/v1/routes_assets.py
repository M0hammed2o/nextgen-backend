"""
Assets routes — centralized image/file management via Supabase Storage.

Flow:
1. Frontend calls POST /upload-url → gets presigned upload URL + asset_id
2. Frontend uploads file directly to Supabase Storage via that URL
3. Frontend attaches asset_id to entity via PUT (e.g. PUT /menu/items/{id})
4. Frontend calls GET /assets/{asset_id}/signed-url → temporary view URL

DB stores asset_id + storage_path. Never raw URLs or blobs.
"""

import logging
import uuid

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.asset import Asset

logger = logging.getLogger("nextgen.assets")
router = APIRouter(prefix="/business/assets", tags=["assets"])
settings = get_settings()

# ── Allowed content types ────────────────────────────────────────────────────

ALLOWED_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
}
MAX_FILE_SIZE_MB = 5


# ── Schemas ──────────────────────────────────────────────────────────────────

class UploadUrlRequest(BaseModel):
    kind: str = Field(
        description="MENU_ITEM_IMAGE | BUSINESS_LOGO | SPECIAL_IMAGE"
    )
    entity_id: uuid.UUID | None = None
    content_type: str = Field(
        default="image/png",
        pattern=r"^image/(png|jpeg|webp|gif)$",
    )
    original_filename: str | None = None


class UploadUrlResponse(BaseModel):
    upload_url: str
    storage_path: str
    asset_id: uuid.UUID


class SignedUrlResponse(BaseModel):
    signed_url: str
    expires_in: int = Field(description="URL validity in seconds")


# ── Supabase Storage Client ─────────────────────────────────────────────────

class SupabaseStorageClient:
    """Thin wrapper around Supabase Storage REST API."""

    def __init__(self):
        self.base_url = settings.SUPABASE_URL
        self.service_key = settings.SUPABASE_SERVICE_KEY
        self.bucket = settings.SUPABASE_STORAGE_BUCKET

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.service_key}",
            "apikey": self.service_key,
        }

    async def create_signed_upload_url(self, storage_path: str) -> str:
        """
        Create a presigned URL for uploading to Supabase Storage.
        Uses the /object/upload/sign endpoint.
        """
        url = (
            f"{self.base_url}/storage/v1/object/upload/sign"
            f"/{self.bucket}/{storage_path}"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self._headers)
            if resp.status_code != 200:
                logger.error(
                    "Supabase upload URL creation failed: %s %s",
                    resp.status_code, resp.text,
                )
                raise AppError(
                    "STORAGE_ERROR",
                    "Failed to create upload URL",
                    status_code=502,
                )
            data = resp.json()
            # Supabase returns a relative signed URL; make it absolute
            signed_path = data.get("url", "")
            return f"{self.base_url}/storage/v1{signed_path}"

    async def create_signed_view_url(
        self, storage_path: str, expires_in: int = 3600
    ) -> str:
        """
        Create a temporary signed URL for viewing/downloading a file.
        Uses the /object/sign endpoint.
        """
        url = f"{self.base_url}/storage/v1/object/sign/{self.bucket}/{storage_path}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers,
                json={"expiresIn": expires_in},
            )
            if resp.status_code != 200:
                logger.error(
                    "Supabase sign URL failed: %s %s",
                    resp.status_code, resp.text,
                )
                raise AppError(
                    "STORAGE_ERROR",
                    "Failed to create signed view URL",
                    status_code=502,
                )
            data = resp.json()
            signed_path = data.get("signedURL", "")
            return f"{self.base_url}/storage/v1{signed_path}"

    async def delete_object(self, storage_path: str) -> None:
        """Delete an object from storage."""
        url = f"{self.base_url}/storage/v1/object/{self.bucket}"
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                url,
                headers=self._headers,
                json={"prefixes": [storage_path]},
            )
            if resp.status_code not in (200, 204):
                logger.warning("Supabase delete failed: %s", resp.text)


def _get_storage_client() -> SupabaseStorageClient:
    return SupabaseStorageClient()


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    body: UploadUrlRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a presigned upload URL for Supabase Storage.

    Flow:
    1. Frontend calls this endpoint → gets upload_url + asset_id
    2. Frontend PUTs the file directly to upload_url
    3. Frontend attaches asset_id to the entity via PUT endpoint
       (e.g. PUT /v1/business/menu/items/{id} with image_asset_id)
    """
    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise AppError(
            "INVALID_CONTENT_TYPE",
            f"Allowed types: {', '.join(ALLOWED_CONTENT_TYPES)}",
            status_code=422,
        )

    # Build storage path: {business_id}/{kind}/{asset_id}.{ext}
    ext = body.content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    asset_id = uuid.uuid4()
    storage_path = f"{user.business_id}/{body.kind.lower()}/{asset_id}.{ext}"

    # Create asset record in DB
    asset = Asset(
        id=asset_id,
        business_id=user.business_id,
        kind=body.kind,
        entity_id=body.entity_id,
        storage_path=storage_path,
        content_type=body.content_type,
        original_filename=body.original_filename,
    )
    db.add(asset)
    await db.commit()

    # Get presigned upload URL from Supabase
    storage = _get_storage_client()
    try:
        upload_url = await storage.create_signed_upload_url(storage_path)
    except AppError:
        # Fallback: construct direct URL (works with service key auth on frontend)
        upload_url = (
            f"{settings.SUPABASE_URL}/storage/v1/object/"
            f"{settings.SUPABASE_STORAGE_BUCKET}/{storage_path}"
        )

    return UploadUrlResponse(
        upload_url=upload_url,
        storage_path=storage_path,
        asset_id=asset_id,
    )


@router.get("/{asset_id}/signed-url", response_model=SignedUrlResponse)
async def get_signed_view_url(
    asset_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
    expires_in: int = 3600,
):
    """
    Get a temporary signed URL to view/display an asset.
    Default expiry: 1 hour. Frontend should cache and refresh as needed.
    """
    result = await db.execute(
        select(Asset).where(
            Asset.id == asset_id,
            Asset.business_id == user.business_id,
        )
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", str(asset_id))

    storage = _get_storage_client()
    signed_url = await storage.create_signed_view_url(
        asset.storage_path, expires_in=expires_in
    )

    return SignedUrlResponse(signed_url=signed_url, expires_in=expires_in)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete an asset from both DB and Supabase Storage.
    Does NOT automatically unlink from entities — caller should
    clear image_asset_id on the entity separately.
    """
    result = await db.execute(
        select(Asset).where(
            Asset.id == asset_id,
            Asset.business_id == user.business_id,
        )
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", str(asset_id))

    # Delete from storage (best-effort)
    storage = _get_storage_client()
    try:
        await storage.delete_object(asset.storage_path)
    except Exception:
        logger.warning("Failed to delete asset from storage: %s", asset.storage_path)

    # Delete DB record
    await db.delete(asset)
    await db.commit()
