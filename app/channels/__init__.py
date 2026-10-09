"""Channel adapters package for omnichannel messaging."""

from typing import Sequence


def format_channel_reply(reply: str, options: Sequence[str] | None = None) -> str:
    """
    Formats the orchestrator reply and any options into plain text suitable
    for messaging platforms such as Facebook Messenger and Instagram DM.
    """
    clean_reply = (reply or "").strip()
    if not options:
        return clean_reply

    options_list = [str(opt).strip() for opt in options if str(opt).strip()]
    if not options_list:
        return clean_reply

    formatted_options = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options_list))
    return f"{clean_reply}\n\n{formatted_options}"
