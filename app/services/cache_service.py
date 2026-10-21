"""
Safe Semantic / Knowledge Cache for Omnichannel Lead Bot.

Provides sub-millisecond responses for repeated general knowledge / FAQ questions
while strictly preventing cross-user data leaks and excluding personalized lead data.
"""
import hashlib
import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# PII and Personalization detectors to prevent caching any sensitive data
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
_PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_PERSONAL_PRONOUNS = re.compile(r"\b(?:my\s+name\s+is|call\s+me|my\s+email|my\s+phone|i\s+live\s+in)\b", re.I)


class SafeKnowledgeCache:
    """
    In-memory semantic & FAQ cache with strict privacy and expiration guardrails.
    Never caches lead data, user answers, or personalized responses.
    """

    def __init__(self, default_ttl_seconds: int = 3600, max_entries: int = 500):
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def _normalize_key(self, tenant_id: str, query: str) -> str:
        """Normalizes and hashes tenant and query."""
        cleaned = re.sub(r"[^\w\s]", "", query.lower()).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        raw_key = f"{tenant_id}:{cleaned}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def is_safe_to_cache(self, query: str, reply: str) -> bool:
        """
        Enforces strict privacy guardrails:
        1. Query and reply must NOT contain emails or phone numbers.
        2. Query must NOT contain self-identifying statements.
        3. Reply must NOT contain dynamic personal placeholders.
        """
        combined = f"{query} {reply}"
        if _EMAIL_PATTERN.search(combined):
            return False
        if _PHONE_PATTERN.search(combined):
            return False
        if _PERSONAL_PRONOUNS.search(query):
            return False
        if any(token in reply for token in ("{booking_slot}", "{contact_info}", "{name}", "{email}", "{phone}")):
            return False
        return True

    def get(self, tenant_id: str, query: str) -> tuple[str, list[str]] | None:
        """Look up cached response. Returns (reply, options) or None."""
        key = self._normalize_key(tenant_id, query)
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None
            if now > entry["expires_at"]:
                del self._cache[key]
                self.misses += 1
                return None
            self.hits += 1
            logger.info("[Cache HIT] Serving cached reply in <1ms for query=%r (hits=%d)", query[:60], self.hits)
            return entry["reply"], list(entry["options"])

    def set(self, tenant_id: str, query: str, reply: str, options: list[str], ttl_seconds: int | None = None) -> None:
        """Stores query and reply if safe."""
        if not self.is_safe_to_cache(query, reply):
            logger.debug("[Cache SKIP] Query or reply contains potentially personalized data: %r", query[:60])
            return

        key = self._normalize_key(tenant_id, query)
        ttl = ttl_seconds or self._default_ttl
        now = time.time()
        with self._lock:
            if len(self._cache) >= self._max_entries:
                # Evict oldest entry
                oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["created_at"])
                del self._cache[oldest_key]

            self._cache[key] = {
                "reply": reply,
                "options": list(options),
                "created_at": now,
                "expires_at": now + ttl,
            }

    def clear(self) -> None:
        """Clears the entire cache."""
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0


# Global singleton instance
knowledge_cache = SafeKnowledgeCache()
