"""
Delivery marketplace webhook — POST /v1/webhook/delivery/{provider}

PUBLIC, UNAUTHENTICATED BY DESIGN — NO TENANT COMPONENT ANYWHERE
----------------------------------------------------------------------
Same public-router pattern as `routes_webhook.py` (Meta) — no `require_*`
RBAC dependency, since the caller is a provider's server, not a logged-in
user. Unlike the PAYMENT webhook route (`POST /payments/webhooks/{provider}
/{business_id}`, safe because each business has its own signing secret
bound to that URL), `{provider}` is the ONLY path parameter here.
Uber signs with one platform-wide client secret, so a valid signature only
proves "Uber sent this," never "this belongs to tenant X" — putting a
business id in this URL would be an unauthenticated, client-suppliable
tenant selector, exactly the anti-pattern the platform's tenant-isolation
rule exists to prevent. `business_id` is resolved EXCLUSIVELY inside
`intake.py` via `(provider, external_store_id) -> delivery_connections`.
See `docs/DELIVERY_PLATFORM_ARCHITECTURE.md` §4.

RAW BODY BEFORE ANY PARSING
--------------------------------
`await request.body()` is read FIRST, and those exact raw bytes are what
get signature-verified and handed to `ingest_delivery_webhook` — the
signature is over the raw body, not a re-serialised parse of it. This
mirrors the Meta webhook handler's own "read body, then verify, then parse"
ordering.

RATE LIMITING — FIXED SCOPE, NOT THE SLOWAPI PATH-DEFAULT (BUG-0077)
-------------------------------------------------------------------------
`{provider}` is a path parameter. BUG-0077 documented that slowapi derives
a limit's bucket from `request["path"]` UNLESS the limit carries an
explicit `scope` — for a route whose path embeds a value, that means every
DISTINCT value gets its own independent rate-limit budget, which is
precisely the traffic shape a hostile sender wants (walk through provider
names, or in the receipt-token case, walk through tokens, to always get a
fresh bucket). `limiter.shared_limit(..., scope=_RATE_LIMIT_SCOPE)` is used
here — NOT `limiter.limit(...)`, which accepts no `scope` argument at all —
so every request to this route, for ANY `{provider}` value, shares ONE
bucket per client IP.

ALWAYS 200, EXCEPT A GENUINE ENQUEUE FAILURE
-------------------------------------------------
Uber requires an ack; `WebhookGateResult.http_status` is 200 for every
signature/tenant/entitlement failure gate (still logged with a specific
`error_category`, never swallowed) and only a real internal enqueue failure
returns something else, so a genuine paid order is never silently dropped.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.ratelimit import limiter
from backend.app.db.session import get_db
from backend.app.delivery.intake import ingest_delivery_webhook

logger = logging.getLogger("nextgen.delivery.webhook")

router = APIRouter(prefix="/webhook/delivery", tags=["delivery-webhook"])

# Fixed rate-limit scope (see module docstring / BUG-0077) — every request
# to ANY provider under this route shares ONE bucket per client IP.
_RATE_LIMIT_SCOPE = "delivery_webhook"
settings = get_settings()


@router.post("/{provider}")
@limiter.shared_limit(
    f"{settings.DELIVERY_WEBHOOK_RATE_LIMIT_PER_MINUTE}/minute",
    scope=_RATE_LIMIT_SCOPE,
)
async def delivery_webhook(
    provider: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive an inbound delivery-marketplace webhook. See module docstring
    for the trust-boundary and rate-limit-scope rules this implements.
    """
    raw_body = await request.body()

    logger.warning(
        "DELIVERY_WEBHOOK_ENTRY: provider=%s body_len=%d client_ip=%s",
        provider, len(raw_body), request.client.host if request.client else "unknown",
    )

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        # No worker pool configured at all (e.g. a misconfigured deployment)
        # — this IS an internal failure, so 503 rather than a false 200 ack.
        logger.error("DELIVERY_WEBHOOK_NO_ARQ_POOL: provider=%s — cannot enqueue.", provider)
        response.status_code = 503
        return {"status": "service_unavailable"}

    result = await ingest_delivery_webhook(
        db, provider=provider, raw_body=raw_body, headers=request.headers, arq_pool=arq_pool,
    )

    response.status_code = result.http_status
    logger.warning(
        "DELIVERY_WEBHOOK_RESULT: provider=%s accepted=%s http_status=%d error_category=%s",
        provider, result.accepted, result.http_status, result.error_category,
    )
    return {
        "status": "accepted" if result.accepted else "rejected",
        "error_category": result.error_category,
    }
