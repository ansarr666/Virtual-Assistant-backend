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

import httpx

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


def generate_openrouter(prompt: str, operation: str, config: types.GenerateContentConfig | None = None) -> str | None:
    """Fallback generator using OpenRouter when Gemini is unavailable or in cooldown."""
    settings = get_settings()
    api_key = (settings.OPENROUTER_API_KEY or "").strip()
    if not api_key:
        return None

    t0 = time.perf_counter()
    logger.info("[AI-OpenRouter] %s fallback start", operation)
    url = f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://orionsoft.tech",
        "X-Title": "OrionSoft Omnichannel Lead Bot",
    }
    payload: dict[str, Any] = {
        "model": settings.OPENROUTER_MODEL.strip(),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": getattr(config, "temperature", 0.1) if config else 0.1,
    }
    if config and getattr(config, "response_mime_type", "") == "application/json":
        payload["response_format"] = {"type": "json_object"}

    try:
        timeout = float(settings.OPENROUTER_TIMEOUT_SECONDS)
        with httpx.Client(timeout=timeout) as http_client:
            resp = http_client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if choices and choices[0].get("message"):
                text = (choices[0]["message"].get("content") or "").strip()
                logger.info("[AI-OpenRouter] %s succeeded in %.2fs", operation, time.perf_counter() - t0)
                return text
        logger.warning(
            "[AI-OpenRouter] %s returned HTTP %s in %.2fs: %s",
            operation, resp.status_code, time.perf_counter() - t0, resp.text[:200],
        )
    except Exception as e:
        logger.warning(
            "[AI-OpenRouter] %s failed after %.2fs: %s: %s",
            operation, time.perf_counter() - t0, type(e).__name__, str(e)[:200],
        )
    return None


def generate(client: Any, prompt: str, operation: str, config: types.GenerateContentConfig) -> str | None:
    """Runs one Gemini call with automatic OpenRouter fallback on failure/cooldown."""
    if client is None or in_cooldown():
        if in_cooldown():
            logger.info("[AI] %s skipped (Gemini cooldown active) -> trying OpenRouter fallback", operation)
        return generate_openrouter(prompt, operation, config)

    t0 = time.perf_counter()
    logger.info("[AI] %s start", operation)
    try:
        response = client.models.generate_content(model=model_name(), contents=prompt, config=config)
        text = (response.text or "").strip()
        logger.info("[AI] %s done in %.2fs", operation, time.perf_counter() - t0)
        return text
    except Exception as e:
        logger.warning(
            "[AI] %s failed after %.2fs: %s: %s -> falling back to OpenRouter",
            operation, time.perf_counter() - t0, type(e).__name__, str(e)[:300],
        )
        if _is_quota_or_outage(e):
            _start_cooldown(f"{type(e).__name__}: {str(e)[:120]}")
        return generate_openrouter(prompt, operation, config)

