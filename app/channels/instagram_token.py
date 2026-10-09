"""
Keeps the Instagram (Instagram Login) access token alive.

Instagram Login tokens last ~60 days and can be refreshed (once they are at least 24 hours
old) via GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token.
The refreshed token is applied to the running Instagram channel and cached in
IG_TOKEN_CACHE_FILE, so a restart keeps using it. The cache is tied to the token in the
environment (by a hash, never the token itself): replacing IG_ACCESS_TOKEN makes the old cache
irrelevant automatically. Tokens are never logged.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
# httpx logs full request URLs at INFO; the refresh endpoint takes the token in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

REFRESH_INTERVAL_SECONDS = 7 * 24 * 3600  # well inside the 60-day lifetime
RETRY_AFTER_ERROR_SECONDS = 6 * 3600


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_path() -> Path:
    return Path(get_settings().IG_TOKEN_CACHE_FILE)


def load_cached_token(env_token: str) -> str | None:
    """Returns a previously refreshed token if it was derived from the current env token and is unexpired."""
    path = _cache_path()
    if not env_token or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Instagram token cache %s is unreadable; ignoring it.", path)
        return None
    if data.get("source_fingerprint") != _fingerprint(env_token):
        return None
    if float(data.get("expires_at", 0)) <= time.time():
        return None
    return data.get("access_token") or None


def _save_cache(env_token: str, token: str, expires_in: int) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_fingerprint": _fingerprint(env_token),
            "access_token": token,
            "refreshed_at": time.time(),
            "expires_at": time.time() + int(expires_in or 0),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    except OSError as e:
        logger.warning("Could not write Instagram token cache %s: %s", path, e)


async def refresh_instagram_token(channel: Any, client: httpx.AsyncClient | None = None) -> bool:
    """Refreshes the channel's token once. Returns True on success; errors are logged, never raised."""
    settings = get_settings()
    env_token = settings.IG_ACCESS_TOKEN
    current = getattr(channel, "access_token", "")
    if not current:
        return False
    url = f"{settings.IG_GRAPH_API_URL.rstrip('/')}/refresh_access_token"
    params = {"grant_type": "ig_refresh_token", "access_token": current}
    try:
        if client is not None:
            resp = await client.get(url, params=params, timeout=20)
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.get(url, params=params, timeout=20)
        data = resp.json()
    except Exception as e:
        logger.warning("Instagram token refresh failed: %s", type(e).__name__)
        return False
    if resp.status_code >= 400 or "access_token" not in data:
        err = data.get("error") or {}
        logger.warning(
            "Instagram token refresh not applied: status=%s code=%s message=%s",
            resp.status_code, err.get("code"), err.get("message"),
        )
        return False
    expires_in = int(data.get("expires_in") or 0)
    channel.access_token = data["access_token"]
    _save_cache(env_token or current, data["access_token"], expires_in)
    logger.info("Instagram token refreshed: valid for %.0f more days.", expires_in / 86400)
    return True


def apply_cached_token(channel: Any) -> None:
    """At startup: use a cached refreshed token if it belongs to the current IG_ACCESS_TOKEN."""
    cached = load_cached_token(get_settings().IG_ACCESS_TOKEN)
    if cached:
        channel.access_token = cached
        logger.info("Instagram: using the auto-refreshed token from the token cache.")


async def instagram_token_keeper(channel: Any) -> None:
    """Background loop: refresh now, then every REFRESH_INTERVAL_SECONDS."""
    while True:
        ok = await refresh_instagram_token(channel)
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS if ok else RETRY_AFTER_ERROR_SECONDS)
