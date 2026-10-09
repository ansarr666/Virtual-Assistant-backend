import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.models.schemas import ChatRequest, ChatResponse
from app.repositories.base import RepositoryError
from app.services.orchestrator import process_message

logger = logging.getLogger(__name__)

router = APIRouter()


# Sync handler: FastAPI runs it in a worker thread, so slow Sheets/Gemini calls
# never block the event loop.
@router.post("/chat", response_model=ChatResponse)
def chat(data: ChatRequest):
    try:
        return process_message(data)
    except RepositoryError as e:
        logger.error(
            "/chat persistence unavailable: tenant=%s user=%s client_message_id=%s error=%s",
            data.tenant_id, data.user_id, data.client_message_id, e,
        )
        # 503 + Retry-After: the client may retry with the same client_message_id.
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={"detail": RepositoryError.public_message, "error": "persistence_unavailable"},
        )
