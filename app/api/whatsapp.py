import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Query, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

import app.config  # noqa: F401  (loads .env)
from app.api.meta_webhook import (  # noqa: F401
    _first_time_seen,
    _process_events,
    normalize_whatsapp_payload,
    read_verified_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/webhooks/whatsapp",
    tags=["WhatsApp"]
)

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN") or os.getenv("META_VERIFY_TOKEN")


# Meta uses this once to verify our webhook
@router.get("")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge")
):
    if (
        VERIFY_TOKEN
        and hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
    ):
        return PlainTextResponse(
            content=hub_challenge
        )

    raise HTTPException(
        status_code=403,
        detail="Webhook verification failed"
    )


# Incoming WhatsApp messages will arrive here
@router.post("")
async def receive_whatsapp_message(request: Request, background_tasks: BackgroundTasks):
    payload, error = await read_verified_json(request, "WhatsApp")
    if error is not None:
        return error

    logger.debug("WhatsApp webhook payload received: %s", payload)
    events = normalize_whatsapp_payload(payload)
    new_events = [e for e in events if _first_time_seen(e.get("client_message_id"))]
    logger.info(
        "WhatsApp webhook event received: messages=%d new=%d", len(events), len(new_events),
    )
    # Acknowledge immediately; process and reply in the background (same path as Meta).
    if new_events:
        background_tasks.add_task(_process_events, new_events)
    return {"status": "ok", "processed": len(new_events)}