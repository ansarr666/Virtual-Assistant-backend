"""
Lazy Google Sheets client.

Nothing here touches the network or credentials at import time, so the API can
start (and serve its health endpoint) even when Google credentials are missing.
Authentication happens on first use; failures raise RepositoryError with a
message explaining how to fix the configuration.
"""
import logging
from pathlib import Path
import threading
from typing import Any

import google.auth
import gspread
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from gspread.http_client import BackOffHTTPClient

from app.config import get_settings
from app.repositories.base import RepositoryError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

_lock = threading.Lock()
_client: gspread.Client | None = None
_spreadsheet: Any = None
_WORKSHEET_CACHE: dict[str, Any] = {}


def _service_account_info(raw: str) -> dict:
    """GOOGLE_SERVICE_ACCOUNT_JSON may hold the key JSON itself or base64 of it."""
    import base64
    import binascii
    import json

    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as e:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor base64-encoded JSON") from e
    return json.loads(text)


def _build_credentials():
    settings = get_settings()
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        from google.oauth2.service_account import Credentials

        info = _service_account_info(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        logger.info("Google Sheets: using service account %s (from GOOGLE_SERVICE_ACCOUNT_JSON)", info.get("client_email"))
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    if settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        from google.oauth2.service_account import Credentials

        logger.info("Google Sheets: using service account file %s", settings.GOOGLE_SERVICE_ACCOUNT_FILE)
        return Credentials.from_service_account_file(settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    token_file = Path(".runtime/google_token.json")
    if token_file.is_file():
        from google.oauth2.credentials import Credentials

        logger.info("Google Sheets: using authorized user token from %s", token_file)
        return Credentials.from_authorized_user_file(str(token_file), scopes=SCOPES)

    # Application Default Credentials: a user login, a service account key via
    # GOOGLE_APPLICATION_CREDENTIALS, or an impersonated service account configured with
    # `gcloud auth application-default login --impersonate-service-account=...`.
    credentials, project = google.auth.default(scopes=SCOPES)
    logger.info(
        "Google Sheets: using Application Default Credentials (%s, project=%s)",
        type(credentials).__module__ + "." + type(credentials).__name__, project,
    )
    return credentials


def get_client() -> gspread.Client:
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            try:
                credentials = _build_credentials()
            except DefaultCredentialsError as e:
                raise RepositoryError(
                    "Google credentials not found. Run `gcloud auth application-default login` "
                    "(optionally with --impersonate-service-account=<sa>) or set "
                    "GOOGLE_SERVICE_ACCOUNT_FILE in .env. Details: " + str(e)
                ) from e
            except (GoogleAuthError, OSError, ValueError) as e:
                raise RepositoryError(f"Could not load Google credentials: {e}") from e
            # BackOffHTTPClient retries 429 / 5xx responses with exponential backoff.
            client = gspread.authorize(credentials, http_client=BackOffHTTPClient)
            client.http_client.set_timeout(get_settings().GOOGLE_SHEETS_TIMEOUT_SECONDS)
            _client = client
    return _client


def get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    sheet_id = get_settings().GOOGLE_SHEET_ID.strip()
    if not sheet_id:
        raise RepositoryError("GOOGLE_SHEET_ID is not set in .env.")
    client = get_client()
    with _lock:
        if _spreadsheet is None:
            try:
                _spreadsheet = client.open_by_key(sheet_id)
            except gspread.exceptions.SpreadsheetNotFound as e:
                raise RepositoryError(
                    f"Spreadsheet {sheet_id} not found or not shared with the authenticated account."
                ) from e
            except Exception as e:
                raise RepositoryError(f"Could not open spreadsheet {sheet_id}: {type(e).__name__}: {e}") from e
            logger.info("Google Sheets: opened spreadsheet '%s'", _spreadsheet.title)
    return _spreadsheet


def get_worksheet(name: str):
    key = name.lower()
    if key in _WORKSHEET_CACHE:
        return _WORKSHEET_CACHE[key]
    spreadsheet = get_spreadsheet()
    worksheet = None
    if hasattr(spreadsheet, "worksheets"):
        try:
            ws_map = {w.title.lower(): w for w in spreadsheet.worksheets()}
            if key in ws_map:
                worksheet = ws_map[key]
            else:
                from app.repositories.google_sheet import SHEET_COLUMNS, _col_letter
                headers = SHEET_COLUMNS.get(key, [])
                cols = max(len(headers) + 5, 20)
                worksheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=cols)
                if headers:
                    worksheet.update([headers], f"A1:{_col_letter(len(headers))}1")
                logger.info("Google Sheets: auto-created missing worksheet '%s'", name)
        except Exception as e:
            logger.warning("Could not list or create worksheet '%s': %s", name, e)

    if worksheet is None:
        try:
            worksheet = spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound as e:
            raise RepositoryError(f"Worksheet '{name}' does not exist in the spreadsheet.") from e
        except Exception as e:
            raise RepositoryError(f"Could not open worksheet '{name}': {type(e).__name__}: {e}") from e

    _WORKSHEET_CACHE[key] = worksheet
    return worksheet


def reset_sheet_client() -> None:
    """Drops cached client/worksheets (e.g. after credentials change)."""
    global _client, _spreadsheet
    with _lock:
        _client = None
        _spreadsheet = None
        _WORKSHEET_CACHE.clear()


class SheetService:
    def __init__(self, repository=None):
        self.repository = repository

    def get_worksheet(self, name: str = "Leads"):
        if self.repository and hasattr(self.repository, "get_worksheet"):
            return self.repository.get_worksheet(name)
        return get_worksheet(name)
