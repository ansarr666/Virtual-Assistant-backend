import logging
from typing import Any

from app.channels.base import MetaMessagingChannel
from app.config import get_settings

logger = logging.getLogger(__name__)


class FacebookMessengerChannel(MetaMessagingChannel):
    """Channel adapter for the Messenger Send API (Page access token, graph.facebook.com)."""

    channel = "facebook"
    display_name = "Facebook"
    max_text_length = 2000
    placeholder_tokens = ("your_page_access_token", "your_access_token_here", "")

    def __init__(
        self,
        page_id: str | None = None,
        page_access_token: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.page_id = page_id if page_id is not None else settings.FB_PAGE_ID
        self._explicit_token = page_access_token
        self.api_version = api_version or settings.META_API_VERSION
        self.base_url = (base_url or settings.META_GRAPH_API_URL).rstrip("/")

    @property
    def page_access_token(self) -> str:
        return self._explicit_token if self._explicit_token is not None else get_settings().FB_PAGE_ACCESS_TOKEN

    @page_access_token.setter
    def page_access_token(self, value: str) -> None:
        self._explicit_token = value

    @property
    def token(self) -> str:
        return self.page_access_token

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/{self.api_version}/me/messages"

    def extra_fields(self) -> dict[str, Any]:
        return {"messaging_type": "RESPONSE"}

    def on_error(self, err: dict[str, Any]) -> None:
        code = err.get("code")
        subcode = err.get("error_subcode")
        if code == 100 and subcode == 33:
            logger.error(
                "Hint: Graph could not resolve '/me' as a Page. FB_PAGE_ACCESS_TOKEN is most likely "
                "a User token, not the Page access token for FB_PAGE_ID=%s.",
                self.page_id,
            )
        elif code == 190:
            logger.error(
                "Hint: Graph returned error 190 (Invalid/Expired OAuth access token). "
                "Verify FB_PAGE_ACCESS_TOKEN is a valid, unexpired Page Access Token for Page ID=%s.",
                self.page_id,
            )
        elif code == 200:
            logger.error(
                "Hint: Graph returned error 200 (Permissions error). In Development Mode, the recipient "
                "must have an Admin, Developer, or Tester role on the Meta App, and the Page token "
                "must have 'pages_messaging' permission."
            )

