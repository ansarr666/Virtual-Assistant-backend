"""
Channel-agnostic option handling shared by the Messenger, Instagram and WhatsApp renderers.

The orchestrator returns options as plain label strings (the option keys of the flow
JSON). Here they are normalized to {"id", "label", "value"}:

- id:    the existing stable slug from intent_service.option_id ("Our Services" -> "our-services").
         Native buttons carry it as their payload; the orchestrator's exact matcher already
         accepts it, exactly like the label the Web UI sends.
- label: what the user sees.
- value: what is fed back into the orchestrator (the label).

Nothing in here knows any business menu; it only shapes whatever options it is given.
"""
import logging
import threading
from collections import OrderedDict
from typing import Any, Sequence

from app.services.intent_service import option_id

logger = logging.getLogger(__name__)

MORE_PREFIX = "more:"  # option_id() only yields [a-z0-9-], so this can never clash with an option
MORE_LABEL = "More options"
MORE_PROMPT = "More options:"
DEFAULT_OPTIONS_PROMPT = "Please choose an option:"
FALLBACK_HINT = "Reply with the option name."
EXPIRED_MENU_REPLY = "That menu is no longer available. Type what you're looking for, or say \"menu\"."

_NAV_EXACT = {"main menu", "menu", "back", "home"}


def normalize_options(
    options: Sequence[Any] | None, display_labels: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """
    Plain strings (or {"label"/"id"} dicts) -> de-duplicated [{"id", "label", "value"}].
    `display_labels` ({label: short label}) adds "short_label" for channels whose button titles
    are length-limited; the id is always derived from the full label, so it never changes.
    """
    display_labels = display_labels or {}
    normalized: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    seen_ids: set[str] = set()
    for opt in options or []:
        if isinstance(opt, dict):
            label = str(opt.get("label") or opt.get("value") or opt.get("id") or "").strip()
            oid = str(opt.get("id") or "").strip() or option_id(label)
        else:
            label = str(opt).strip()
            oid = option_id(label)
        if not label or label in seen_labels:
            continue
        oid = oid or "option"
        base, n = oid, 2
        while oid in seen_ids:  # keep ids unique within one menu
            oid = f"{base}-{n}"
            n += 1
        seen_labels.add(label)
        seen_ids.add(oid)
        item = {"id": oid, "label": label, "value": label}
        short = display_labels.get(label) or (opt.get("short_label") if isinstance(opt, dict) else None)
        if short:
            item["short_label"] = str(short).strip()
        normalized.append(item)
    return normalized


def button_title(option: dict[str, str], limit: int) -> str:
    """The label to show on a length-limited button: the configured short label when the full one is too long."""
    label = option["label"]
    if len(label) > limit and option.get("short_label"):
        return option["short_label"]
    return label


def is_navigation(label: str) -> bool:
    """Generic navigation detection (Main Menu, Back, Back to ...), so paging keeps them visible."""
    text = label.strip().lower()
    return text in _NAV_EXACT or text.startswith(("back to ", "main menu"))


def more_option(page_number: int) -> dict[str, str]:
    return {"id": f"{MORE_PREFIX}{page_number}", "label": MORE_LABEL, "value": ""}


def is_more_selection(selection_id: str | None) -> bool:
    return bool(selection_id) and str(selection_id).startswith(MORE_PREFIX)


def paginate(options: list[dict[str, str]], limit: int) -> list[list[dict[str, str]]]:
    """
    Splits options into pages of at most `limit` entries. Every page keeps the navigation
    options (Main Menu / Back ...) and all but the last page end with a "More options"
    entry, so no option is ever dropped.
    """
    if len(options) <= limit:
        return [options]
    nav = [o for o in options if is_navigation(o["label"])]
    body = [o for o in options if not is_navigation(o["label"])]
    if len(nav) > limit - 2:  # not enough room to pin them all; page everything instead
        nav, body = [], options
    per_page = max(1, limit - len(nav) - 1)
    chunks = [body[i:i + per_page] for i in range(0, len(body), per_page)] or [[]]
    pages = []
    for idx, chunk in enumerate(chunks):
        page = list(chunk)
        if idx < len(chunks) - 1:
            page.append(more_option(idx + 2))
        pages.append(page + nav)
    # The last page has room for one more entry because it has no "More" item.
    return pages


def fallback_text(reply: str, options: list[dict[str, str]]) -> str:
    """Concise numbered text, used only when a native interactive message cannot be sent."""
    reply = (reply or "").strip()
    real = [o for o in options if not is_more_selection(o["id"])]
    if not real:
        return reply
    lines = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(real, 1))
    return f"{reply}\n\n{lines}\n\n{FALLBACK_HINT}".strip()


def split_text(text: str, limit: int) -> list[str]:
    """Splits text into chunks within a platform's length limit, preferring line/word boundaries."""
    text = (text or "").strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class MenuMemory:
    """
    Remembers the last menu rendered to each (channel, recipient): the pages (for "More
    options") and the option ids (to map a tapped id back to its exact label). In-memory and
    bounded; after a restart, taps still work because the id itself is understood by the
    orchestrator.
    """

    def __init__(self, max_entries: int = 2000):
        self._data: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries

    def remember(self, channel: str, recipient: str, pages: list[list[dict[str, str]]]) -> None:
        with self._lock:
            self._data[(channel, str(recipient))] = {"pages": pages}
            self._data.move_to_end((channel, str(recipient)))
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def page(self, channel: str, recipient: str, page_number: int) -> list[dict[str, str]] | None:
        with self._lock:
            entry = self._data.get((channel, str(recipient)))
        if not entry or not (1 <= page_number <= len(entry["pages"])):
            return None
        return entry["pages"][page_number - 1]

    def label_for(self, channel: str, recipient: str, selection_id: str) -> str | None:
        with self._lock:
            entry = self._data.get((channel, str(recipient)))
        if not entry:
            return None
        for page in entry["pages"]:
            for opt in page:
                if opt["id"] == selection_id and opt["value"]:
                    return opt["value"]
        return None

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


menu_memory = MenuMemory()


def parse_more_page(selection_id: str) -> int:
    try:
        return int(str(selection_id)[len(MORE_PREFIX):])
    except ValueError:
        return 0
