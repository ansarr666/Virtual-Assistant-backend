from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute project root, so tenants/flows/.env resolve no matter which
# directory uvicorn is started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Load .env once, early, for modules that still read os.environ directly.
load_dotenv(ENV_FILE, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    API_KEY: str = ""
    LLM_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"
    # Upper bound for a single Gemini call (seconds). The Gemini API rejects deadlines under 10s.
    GEMINI_TIMEOUT_SECONDS: float = 10.0
    # After a quota/availability error, skip Gemini for this many seconds.
    GEMINI_COOLDOWN_SECONDS: float = 300.0

    # Backup AI Provider (OpenRouter)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openai/gpt-4o-mini"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_TIMEOUT_SECONDS: float = 8.0

    # "google_sheets" (default) or "memory" (local development without credentials).
    REPOSITORY_BACKEND: str = "google_sheets"
    GOOGLE_SERVICE_ACCOUNT_FILE: str = ""
    # Service-account key as JSON text (or base64 of it) - for servers where a key file is awkward.
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""
    GOOGLE_SHEET_ID: str = ""
    GOOGLE_SHEET_NAME: str = "Leads"
    GOOGLE_SHEETS_TIMEOUT_SECONDS: float = 15.0
    GOOGLE_APPS_SCRIPT_URL: str = ""

    # Meta / Facebook Messenger / Instagram DM
    # Must be set in .env; webhook verification is refused while it is empty.
    META_VERIFY_TOKEN: str = ""
    META_APP_SECRET: str = ""
    META_API_VERSION: str = "v21.0"
    META_GRAPH_API_URL: str = "https://graph.facebook.com"
    FB_PAGE_ID: str = ""
    FB_PAGE_ACCESS_TOKEN: str = ""
    IG_ACCOUNT_ID: str = ""
    IG_ACCESS_TOKEN: str = ""
    # Instagram Login webhooks are signed with the Instagram app secret (Instagram > API setup
    # with Instagram login), not META_APP_SECRET. Optional until real Instagram webhooks arrive.
    IG_APP_SECRET: str = ""
    # Instagram Login tokens last ~60 days; the API refreshes them automatically and caches the
    # refreshed token here (tied to IG_ACCESS_TOKEN; replacing that token resets the cache).
    IG_TOKEN_AUTO_REFRESH: bool = True
    IG_TOKEN_CACHE_FILE: str = str(PROJECT_ROOT / ".runtime" / "instagram_token.json")
    # Instagram API with Instagram Login (IGAA... tokens) is served from graph.instagram.com.
    IG_GRAPH_API_URL: str = "https://graph.instagram.com"

    # WhatsApp Cloud API
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""

    # OrionSoft External RAG Service
    RAG_API_BASE_URL: str = ""
    RAG_TIMEOUT_SECONDS: float = 10.0
    RAG_API_KEY: str = ""

    # Comma-separated list of browser origins allowed to call the API.
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Regex for browser origins allowed to call the API (e.g. Netlify deployment and preview URLs).
    CORS_ORIGIN_REGEX: str | None = r"https://[a-zA-Z0-9_.-]+\.netlify\.app"
    LOG_LEVEL: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
