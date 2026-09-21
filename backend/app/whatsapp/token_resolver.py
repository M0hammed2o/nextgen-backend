"""
resolve_whatsapp_credentials(db, business) -> WhatsAppCredentials | None

Model 1 fallback rule (DECISION-0022): a business whose ACTIVE routing
number (`Business.whatsapp_phone_number_id`) belongs to a CONNECTED
whatsapp_connections row sends/receives on ITS OWN token (source="business").
Every other business — no connection row at all, or a connection that never
reached CONNECTED — uses the platform token exactly as before Milestone C
(source="platform").

Once a business's routing number DOES belong to a connection row, the
platform token is NEVER used for it again, even if that connection is
NEEDS_ATTENTION or otherwise not CONNECTED — the platform token has no
Graph permissions on a customer-owned WABA, so falling back there would not
recover the send, it would just produce a second, more confusing failure.
This function returns None in that case; callers must treat None as "no
usable credentials for this send" (a hard stop), not "try the platform
token as a substitute". This is also NOT a scenario is_whatsapp_enabled
governs — that flag independently controls the module kill switch and is
checked separately (see routes_whatsapp_connect.py, pipeline.py).

Takes the Business ORM row itself (never a bare phone_number_id or
business_id) so it is structurally impossible to resolve one business's
credentials against another business's request — the caller must already
have loaded the correct tenant row (which every call site in this codebase
already does, scoped from the JWT's business_id).
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.crypto import CredentialDecryptionError, decrypt_credential
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection

logger = logging.getLogger("nextgen.whatsapp.resolver")


@dataclass(frozen=True)
class WhatsAppCredentials:
    phone_number_id: str
    access_token: str
    source: str  # "business" | "platform"
    connection_id: uuid.UUID | None = None


async def resolve_whatsapp_credentials(
    db: AsyncSession, business: Business
) -> WhatsAppCredentials | None:
    """
    Resolve which phone_number_id/token to send/receive with for `business`.

    Returns None when there are no usable credentials at all: either (a) the
    business's active routing number belongs to a connection that is not
    CONNECTED (no platform fallback — see module docstring), or (b) there is
    no connection and no legacy whatsapp_phone_number_id configured either.
    """
    settings = get_settings()

    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    connection = result.scalar_one_or_none()

    routing_number = business.whatsapp_phone_number_id
    connection_owns_routing_number = (
        connection is not None
        and routing_number is not None
        and connection.phone_number_id == routing_number
    )

    if connection_owns_routing_number:
        if connection.status != "CONNECTED":
            logger.info(
                "WhatsApp connection for business=%s is %s (not CONNECTED) — "
                "no platform-token fallback (platform token has no rights on "
                "a customer-owned WABA).",
                business.id, connection.status,
            )
            return None
        if not connection.access_token_encrypted:
            logger.error(
                "WhatsApp connection for business=%s is CONNECTED but has no "
                "stored token — treating as no usable credentials.",
                business.id,
            )
            return None
        try:
            token = decrypt_credential(connection.access_token_encrypted)
        except CredentialDecryptionError:
            logger.error(
                "WhatsApp connection token undecryptable for business=%s "
                "connection=%s — treating as no usable credentials.",
                business.id, connection.id,
            )
            return None
        if not token:
            return None
        return WhatsAppCredentials(
            phone_number_id=connection.phone_number_id,
            access_token=token,
            source="business",
            connection_id=connection.id,
        )

    # F2 fix, Wave 0A verifier: a connection row's ids can legitimately
    # diverge from businesses.whatsapp_phone_number_id mid-reconnect (the
    # new number is only committed onto the connection row once Meta
    # confirms ownership — see connection_service.run_exchange's F1 fix —
    # but a FAILED reconnect can still leave connection.phone_number_id
    # pointed at a still-unverified target while the business's live
    # routing number is unchanged). Falling through to the platform-token
    # branch in that case would route a customer-owned number through a
    # token that has no Graph rights on it — a guaranteed, confusing
    # failure, not a working fallback. So: ANY connection row that is not
    # DISCONNECTED means this business is under self-service WhatsApp
    # management and must never receive a platform-token fallback, even if
    # its ids don't currently match. Same for a business whose WABA was
    # ever claimed via a customer connection (businesses.
    # whatsapp_business_account_id set) even if the connection row itself
    # is gone — disconnect() clears this field, so it only stays set for a
    # business that is still, or was left, mid-connection.
    if connection is not None and connection.status != "DISCONNECTED":
        logger.info(
            "WhatsApp connection for business=%s is %s and does not own the "
            "current routing number — no platform-token fallback while a "
            "non-DISCONNECTED customer connection exists.",
            business.id, connection.status,
        )
        return None
    if business.whatsapp_business_account_id:
        logger.info(
            "Business=%s has a customer WABA on record (%s) but no active "
            "connection row — no platform-token fallback.",
            business.id, business.whatsapp_business_account_id,
        )
        return None

    # No connection owns this business's routing number — Model 1
    # platform-token path, unchanged from pre-Milestone-C behaviour.
    if routing_number:
        return WhatsAppCredentials(
            phone_number_id=routing_number,
            access_token=settings.WHATSAPP_DEFAULT_ACCESS_TOKEN,
            source="platform",
        )
    return None
