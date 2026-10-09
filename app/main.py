import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import meta_webhook
from app.api.chat import router as chat_router
from app.api.meta_webhook import router as meta_router
from app.api.whatsapp import router as whatsapp_router
from app.channels.instagram_token import apply_cached_token, instagram_token_keeper
from app.config import get_settings
from app.repositories.base import RepositoryError
from app.services import orchestrator
from app.services.flow_service import validate_all_flows
from app.services.tenant_service import list_tenants

settings = get_settings()


def _configure_logging() -> None:
    # Uvicorn only configures its own loggers; without this, every app.* INFO log is dropped.
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(settings.LOG_LEVEL.upper())


_configure_logging()
# Never log outbound request URLs (Graph API calls can carry tokens in query strings).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("app.main")


def _validate_flows_on_startup() -> None:
    results = validate_all_flows()
    used = {t.get("flow") for t in list_tenants()}
    for name, errors in results.items():
        if errors:
            level = logging.ERROR if name in used else logging.WARNING
            logger.log(level, "Flow '%s' is INVALID (%d errors):\n  - %s", name, len(errors), "\n  - ".join(errors))
        else:
            logger.info("Flow '%s' validated OK", name)
    for flow in used - set(results):
        logger.error("Tenant references missing flow file '%s'", flow)


def _warm_up_repository() -> None:
    repo = orchestrator.get_repository()
    try:
        repo.warm_up()
    except RepositoryError as e:
        logger.error("Persistence warm-up FAILED - /chat will return 503 until this is fixed: %s", e)
    except Exception:
        logger.exception("Persistence warm-up failed unexpectedly")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "Starting API: repository=%s, gemini=%s, cors_origins=%s",
        settings.REPOSITORY_BACKEND, "configured" if settings.GEMINI_API_KEY else "disabled",
        settings.cors_origin_list,
    )
    _validate_flows_on_startup()
    # Warm up persistence in the background: startup and GET / stay instant, and the
    # first chat message doesn't pay for authentication + sheet loading.
    threading.Thread(target=_warm_up_repository, name="repo-warm-up", daemon=True).start()

    # Keep the Instagram token alive (it expires after ~60 days unless refreshed).
    ig_task = None
    ig_channel = meta_webhook.instagram_channel
    if settings.IG_TOKEN_AUTO_REFRESH and ig_channel.access_token:
        apply_cached_token(ig_channel)
        ig_task = asyncio.create_task(instagram_token_keeper(ig_channel))
    try:
        yield
    except asyncio.CancelledError:
        # A forced stop (double Ctrl+C, or --reload restarting on Windows) cancels the
        # lifespan while it waits for shutdown; that's a normal stop, not an error.
        logger.info("API stopped (forced shutdown/reload).")
    finally:
        if ig_task is not None:
            ig_task.cancel()


app = FastAPI(title="Omnichannel Conversational Lead Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "X-Request-ID"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Logged when the request ARRIVES (uvicorn's access log only prints after completion),
    # so a slow or aborted request is still visible in the terminal.
    request_id = request.headers.get("x-request-id") or uuid4().hex[:8]
    t0 = time.perf_counter()
    if request.method != "OPTIONS":
        logger.info("--> %s %s (req=%s origin=%s)", request.method, request.url.path, request_id,
                    request.headers.get("origin", "-"))
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s (req=%s)", request.method, request.url.path, request_id)
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Please try again."},
        )
    if request.method != "OPTIONS":
        logger.info("<-- %s %s %s in %.3fs (req=%s)", request.method, request.url.path, response.status_code,
                    time.perf_counter() - t0, request_id)
    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(chat_router)
app.include_router(meta_router)
app.include_router(whatsapp_router)


@app.get("/")
def health():
    return {
        "status": "ok"
    }


@app.get("/health")
def health_details():
    """Readiness details for debugging. Does not call Google APIs."""
    flows = validate_all_flows()
    return {
        "status": "ok",
        "repository": settings.REPOSITORY_BACKEND,
        "google_sheet_configured": bool(settings.GOOGLE_SHEET_ID),
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "flows": {name: ("ok" if not errs else f"{len(errs)} errors") for name, errs in flows.items()},
        "cors_origins": settings.cors_origin_list,
        # Booleans only - never values.
        "channels": {
            "webhook_signature_check": bool(settings.META_APP_SECRET),
            "facebook": bool(settings.FB_PAGE_ID and meta_webhook.facebook_channel.is_configured),
            "instagram": bool(meta_webhook.instagram_channel.is_configured),
            "instagram_signature_secret": bool(settings.IG_APP_SECRET),
            "whatsapp": bool(meta_webhook.whatsapp_channel.is_configured),
        },
    }
