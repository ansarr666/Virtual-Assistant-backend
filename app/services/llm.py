"""
Shared Gemini access with a hard timeout and a cooldown circuit breaker.

Gemini is an optional enhancement: every caller must work without it. When a call
fails with a quota/availability error, further calls are skipped for
GEMINI_COOLDOWN_SECONDS so users don't pay the latency of a request we know will fail.
"""
import logging
import threading
import time
from typing import Any

from google import genai
from google.genai import types

from app.config import get_settings

logger = logging.getLogger(__name__)

_cooldown_until = 0.0
_cooldown_lock = threading.Lock()


def create_client() -> Any:
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY not set: AI matching disabled, deterministic routing only.")
        return None
    try:
        return genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                # Gemini returns 400 INVALID_ARGUMENT for deadlines below 10s.
                timeout=int(max(settings.GEMINI_TIMEOUT_SECONDS, 10.0) * 1000),
                # Don't retry: the conversation falls back deterministically instead.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    except Exception as e:
        logger.warning("Could not initialize Gemini client: %s", e)
        return None


def model_name() -> str:
    return get_settings().GEMINI_MODEL.strip()


def in_cooldown() -> bool:
    return time.monotonic() < _cooldown_until


def reset_cooldown() -> None:
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = 0.0


def _start_cooldown(reason: str) -> None:
    global _cooldown_until
    seconds = get_settings().GEMINI_COOLDOWN_SECONDS
    with _cooldown_lock:
        _cooldown_until = time.monotonic() + seconds
    logger.warning("Gemini disabled for %.0fs after error: %s", seconds, reason)


def _is_quota_or_outage(error: Exception) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(error)
    return any(s in text for s in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "timed out", "Timeout", "ConnectError"))


def generate(client: Any, prompt: str, operation: str, config: types.GenerateContentConfig) -> str | None:
    """Runs one Gemini call. Returns response text, or None on any failure (never raises)."""
    if client is None:
        return None
    if in_cooldown():
        logger.info("[AI] %s skipped (cooldown active)", operation)
        return None

    t0 = time.perf_counter()
    logger.info("[AI] %s start", operation)
    try:
        response = client.models.generate_content(model=model_name(), contents=prompt, config=config)
        text = (response.text or "").strip()
        logger.info("[AI] %s done in %.2fs", operation, time.perf_counter() - t0)
        return text
    except Exception as e:
        logger.warning(
            "[AI] %s failed after %.2fs: %s: %s",
            operation, time.perf_counter() - t0, type(e).__name__, str(e)[:300],
        )
        if _is_quota_or_outage(e):
            _start_cooldown(f"{type(e).__name__}: {str(e)[:120]}")
        return None
