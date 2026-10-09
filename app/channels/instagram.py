import logging

from app.channels.base import MetaMessagingChannel
from app.config import get_settings

logger = logging.getLogger(__name__)


class InstagramChannel(MetaMessagingChannel):
    """
    Channel adapter for Instagram DMs via the Instagram API with Instagram Login
    (IGAA... tokens): POST graph.instagram.com/{version}/me/messages, where "me" is the
    Instagram professional account that owns IG_ACCESS_TOKEN. Supports text + quick replies.
    """

    channel = "instagram"
    display_name = "Instagram"
    max_text_length = 1000  # Instagram DM text limit
    placeholder_tokens = ("your_instagram_access_token", "your_access_token_here", "")

    def __init__(
        self,
        account_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.account_id = account_id if account_id is not None else settings.IG_ACCOUNT_ID
        self.access_token = access_token if access_token is not None else settings.IG_ACCESS_TOKEN
        self.api_version = api_version or settings.META_API_VERSION
        self.base_url = (base_url or settings.IG_GRAPH_API_URL).rstrip("/")

    @property
    def token(self) -> str:
        return self.access_token

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/{self.api_version}/me/messages"
