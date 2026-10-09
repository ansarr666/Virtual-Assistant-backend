"""
Intent and option matching.

Deterministic rules run first and handle the common cases with no network call.
Gemini is only consulted for ambiguous free text, and every Gemini failure
degrades to "no match" so the orchestrator can fall back gracefully.
"""
import logging
import re
from typing import Any

from google.genai import types

from app.models.schemas import IntentClassificationResult, OptionMatchResult
from app.services import llm

logger = logging.getLogger(__name__)

MODEL = llm.model_name()
client: Any = llm.create_client()

SERVICE_INTENTS = (
    "WEBSITE", "MOBILE_APP", "CUSTOM_SOFTWARE", "AI_AUTOMATION",
    "CHATBOT", "SHOPIFY_ECOMMERCE", "SEO",
)


def normalize(message: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    text = (message or "").lower().replace("'", "")
    return re.sub(r"[^\w]+", " ", text).strip()


def _has(pattern: str, text: str) -> bool:
    return re.search(rf"(?<!\w)(?:{pattern})(?!\w)", text) is not None


# ----------------------------------------------------------------------
# Acknowledgements ("ok", "thanks", "sounds good", "👍")
# ----------------------------------------------------------------------
_ACK_CORE = {
    "ok", "okay", "okk", "okey", "k", "kk", "thanks", "thank", "thankyou", "thanx", "thnx", "thx", "ty",
    "tysm", "cool", "great", "perfect", "alright", "allright", "done", "sure", "fine", "nice", "awesome",
    "noted", "understood", "gotcha", "got", "good", "appreciate", "appreciated", "cheers", "excellent",
    "wonderful", "amazing", "super", "lovely", "right", "clear", "helpful", "makes", "sense", "useful",
    "neat", "splendid", "fantastic", "brilliant", "sweet", "valid", "kudos", "welcome",
}
_ACK_FILLER = {
    "you", "u", "so", "much", "very", "a", "lot", "for", "the", "that", "this", "info", "information",
    "help", "all", "it", "sounds", "and", "yeah", "yes", "yep", "ya", "haha", "sir", "maam",
    "bro", "dear", "again", "really", "i", "we", "will", "wait", "now", "then",
    "oh", "ooh", "ah", "ahh", "well", "wow", "thats", "there", "details", "explanation", "update",
    "sharing", "clarifying", "response", "gotit", "okie", "okies", "no", "problem", "worries",
    "letting", "know", "to", "see", "hear", "me", "about", "mate", "buddy", "man", "guys", "folks",
    "team", "orionsoft", "answer", "answering", "question", "questions", "quick", "fast", "prompt",
    "kind", "ton", "tons", "million", "day", "night", "week", "bye", "care", "have", "good", "great",
    "awesome", "fair", "enough", "keep", "mind", "think", "talk", "later", "soon", "touch",
    "stay", "safe", "cheers", "assist", "assistance", "everything", "reply", "replies", "message",
    "clarification", "clearing", "up", "glad", "happy", "both", "allgood", "alrighty",
}
_ACK_EMOJI = set("👍🙏😊🙂👌✅🤝😀😄❤💯👏🙌")
_MAX_ACK_WORDS = 20

_ACK_PHRASE_PATTERNS = (
    r"\b(?:thanks?|thank\s+you|thx|ty|tysm|thanx)\b",
    r"\b(?:much\s+appreciated|appreciate\s+(?:it|that|this|the|your))\b",
    r"\b(?:sounds?\s+(?:good|great|awesome|cool|promising|perfect|fine|nice))\b",
    r"\b(?:got\s+it|understood|makes?\s+sense|noted|gotcha|got\s+that)\b",
    r"\b(?:no\s+(?:problem|worries))\b",
)

# Social chitchat / bot identity / pleasantries patterns
_HOW_ARE_YOU_PATTERN = r"\b(?:how\s+(?:are|r)\s+(?:you|u|things|ya)|hows?\s+it\s+going|how\s+do\s+you\s+do|whats?\s+up|how\s+have\s+you\s+been)\b"
_IDENTITY_PATTERN = r"\b(?:who\s+are\s+you|what\s+are\s+you|who\s+is\s+this|are\s+you\s+(?:an?\s+)?(?:ai|bot|robot|human|real\s+person)|what\s+can\s+you\s+do|what\s+do\s+you\s+do|who\s+made\s+you|tell\s+me\s+about\s+yourself)\b"
_COMPLIMENT_PATTERN = r"\b(?:you(?:\s+guys|\s+all)?(?:\s*re|\s+are)\s+(?:awesome|great|cool|the\s+best|so\s+smart|amazing|helpful)|good\s+job|nice\s+(?:work|bot|assistant)|love\s+(?:it|this))\b"
_HESITATION_PATTERN = r"\b(?:i(?:ll|\s+will)\s+think\s+about\s+it|let\s+me\s+think|not\s+sure\s+yet|not\s+sure\s+right\s+now|maybe\s+later|just\s+(?:looking|browsing)|give\s+me\s+a\s+(?:moment|sec|minute)|let\s+me\s+check\s+with\s+(?:my\s+)?team|will\s+get\s+back\s+to\s+you)\b"
_FAREWELL_PATTERN = r"\b(?:goodbye|bye\s+bye|bye|see\s+(?:you|ya)|talk\s+soon|talk\s+to\s+you\s+later|take\s+care|have\s+a\s+(?:good|great|nice)\s+day)\b"


def is_acknowledgement(message: str) -> bool:
    """Deterministic: is this message only an acknowledgement / closing courtesy?"""
    raw = (message or "").strip()
    if not raw:
        return False

    # Exclude contact details
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw):
        return False
    if re.search(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)", raw):
        return False

    # Exclude questions
    if "?" in raw:
        return False

    text = normalize(raw)
    # Exclude farewells
    if re.search(r"\b(?:goodbye|bye\s+bye|bye|take\s+care)\b", text):
        return False

    words = text.split()
    if not words:
        return any(ch in _ACK_EMOJI for ch in raw)
    if len(words) > _MAX_ACK_WORDS:
        return False

    # Exclude explicit navigation commands
    if text == "0" or any(text == p for p in ("main menu", "menu", "restart", "start over")):
        return False

    # Exclude service inquiries with buying intent (e.g. "thanks, I also need SEO")
    service = detect_service(raw)
    if service and any(b in words for b in ("need", "needs", "want", "wanted", "build", "create", "develop", "make", "require", "hire", "looking")):
        return False
    # Exclude pricing questions
    if is_pricing_question(raw):
        return False

    # Check 1: All words are in ACK_CORE or ACK_FILLER and at least one is in ACK_CORE
    if all(w in _ACK_CORE or w in _ACK_FILLER for w in words) and any(w in _ACK_CORE for w in words):
        return True

    # Check 2: Matches an explicit acknowledgment phrase pattern
    if any(re.search(pat, text) for pat in _ACK_PHRASE_PATTERNS):
        if not service and not any(b in words for b in ("hire", "quote", "cost", "price", "budget", "build", "develop", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")):
            return True

    return False


def detect_chitchat(message: str) -> dict[str, str] | None:
    """Detects social chitchat, bot identity questions, hesitations, or pleasantries."""
    raw = (message or "").strip()
    if not raw:
        return None
    if detect_handoff_command(raw) or detect_global_command(raw):
        return None
    text = normalize(raw)

    if re.search(_HOW_ARE_YOU_PATTERN, text):
        return {
            "type": "how_are_you",
            "reply": "I'm doing well, thank you for asking! I'm the virtual assistant at OrionSoft Technologies. How can I help you today?",
        }
    if re.search(_IDENTITY_PATTERN, text):
        return {
            "type": "identity",
            "reply": (
                "I'm the AI sales assistant for OrionSoft Technologies! We specialize in Website Development, "
                "Mobile Apps, Custom Software, AI & Automation, and Chatbots. I can help answer questions about "
                "our services, share estimates, or schedule a free discovery call with our team."
            ),
        }
    if re.search(_COMPLIMENT_PATTERN, text):
        return {
            "type": "compliment",
            "reply": "Thank you so much! Our team at OrionSoft works hard to deliver exceptional technology solutions. How can I assist with your project today?",
        }
    if re.search(_HESITATION_PATTERN, text):
        return {
            "type": "hesitation",
            "reply": "No problem at all! Take your time. Feel free to explore our services or reach out whenever you're ready.",
        }
    if re.search(_FAREWELL_PATTERN, text):
        return {
            "type": "farewell",
            "reply": "Goodbye! Have a wonderful day ahead, and feel free to reach back out anytime you need software, web, or AI development assistance!",
        }
    return None


# ----------------------------------------------------------------------
# Global navigation commands
# ----------------------------------------------------------------------
_MENU_PHRASES = (
    "main menu", "back to menu", "back to the menu", "menu", "home",
    "restart", "start over", "start again", "begin again", "start from scratch",
    "from the beginning", "beginning", "go back", "take me back", "back to start", "start",
)
_GREETINGS = (
    "hi there", "hello there", "hey there", "good morning", "good afternoon", "good evening",
    "hi", "hello", "hey", "hiya", "greetings", "yo", "namaste",
)
_FILLER = {
    "please", "pls", "plz", "can", "could", "would", "will", "you", "u", "i", "id", "im", "want",
    "wanna", "like", "to", "the", "me", "us", "show", "take", "bring", "go", "again", "let", "lets",
    "just", "ok", "okay", "now", "a", "an", "back", "return", "get", "see", "open", "display", "there",
    "kindly", "so", "um", "and", "then", "thanks", "thank", "my", "of", "over", "help", "need",
    "with", "all", "options", "option", "choices", "everything", "send", "list",
    "what", "do", "have", "again", "once", "more", "way", "too",
}


def detect_global_command(message: str) -> str | None:
    """
    Deterministic detection of global navigation commands.
    Returns 'MAIN_MENU', 'START', or None.

    A message only counts as a command when, after removing command phrases and
    filler words, nothing else is left. So "can you show me the menu again?" is a
    command, but "a home services booking site" is not.
    """
    text = normalize(message)
    if not text:
        return None
    if text == "0":
        return "MAIN_MENU"

    found_menu = False
    found_greeting = False
    for phrase in _MENU_PHRASES:
        pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
        if re.search(pattern, text):
            found_menu = True
            text = re.sub(pattern, " ", text)
    for phrase in _GREETINGS:
        pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
        if re.search(pattern, text):
            found_greeting = True
            text = re.sub(pattern, " ", text)

    leftover = [w for w in text.split() if w not in _FILLER]
    if leftover:
        return None
    if found_menu:
        return "MAIN_MENU"
    if found_greeting:
        return "START"
    return None


def might_be_navigation_request(message: str) -> bool:
    """Quick cue check to see if a query might be asking for navigation."""
    normalized = normalize(message)
    navigation_cues = (
        "menu", "home", "restart", "start over", "start again",
        "go back", "take me back", "bring me back", "beginning",
        "starting", "return"
    )
    return any(_has(re.escape(cue), normalized) for cue in navigation_cues)


# ----------------------------------------------------------------------
# Handoff / booking
# ----------------------------------------------------------------------
_BOOKING = (
    r"book(?:ing)?\s+(?:a\s+|an\s+)?(?:free\s+)?(?:discovery\s+)?(?:call|meeting|consultation|demo|appointment|slot)",
    r"(?:schedule|arrange|set\s+up|setup)\s+(?:a\s+|an\s+)?(?:free\s+)?(?:discovery\s+)?(?:call|meeting|consultation|demo)",
    r"discovery\s+call", r"calendar\s+slot",
)
_CONTACT_ME = (
    r"(?:contact|call|email|ring|text|whatsapp)\s+me", r"reach\s+out\s+to\s+me",
    r"get\s+in\s+touch\s+with\s+me", r"call\s+me\s+back", r"have\s+someone\s+contact",
)
_HUMAN = (
    r"(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|your\s+|the\s+|some\s+)?(?:someone|somebody|human|person|people|agent|team|sales|salesperson|expert|consultant|developer|representative|rep|manager|real\s+person)",
    r"talk\s+to\s+(?:your\s+)?team", r"human\s+(?:agent|handoff|being)", r"real\s+person",
    r"connect\s+me\s+(?:to|with)", r"sales\s*person", r"customer\s+(?:service|care)",
)


def detect_handoff_command(message: str) -> str | None:
    """
    Detects explicit human handoff or booking requests.
    Returns 'BOOK_CALL', 'CONTACT_ME', 'HUMAN_AGENT', or None.
    """
    text = normalize(message)
    if any(_has(p, text) for p in _BOOKING):
        return "BOOK_CALL"
    if any(_has(p, text) for p in _CONTACT_ME):
        return "CONTACT_ME"
    if any(_has(p, text) for p in _HUMAN):
        return "HUMAN_AGENT"
    return None


# ----------------------------------------------------------------------
# Service / pricing detection
# ----------------------------------------------------------------------
# Order matters: more specific services first ("AI chatbot" is a chatbot, "web app" a website).
_SERVICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("CHATBOT", r"chat\s*bots?|bots?|virtual\s+assistants?|conversational\s+ai|whatsapp\s+(?:bot|automation)|rag"),
    ("SHOPIFY_ECOMMERCE", r"shopify|e\s*commerce|ecommerce|online\s+(?:store|shop)|web\s*shop|woocommerce"),
    ("WEBSITE", r"web\s*sites?|landing\s+pages?|web\s*pages?|web\s+app(?:lication)?s?|wordpress|webflow"),
    ("MOBILE_APP", r"mobile\s+apps?|apps?|android|ios|iphone|flutter|react\s+native|play\s+store|app\s+store"),
    ("SEO", r"seo|digital\s+marketing|marketing|google\s+ads|paid\s+ads|ppc|social\s+media|meta\s+ads|ads\s+campaign"),
    ("CUSTOM_SOFTWARE", r"custom\s+software|software|saas|erp|crm|internal\s+tools?|portal|dashboard|integrations?|api"),
    ("AI_AUTOMATION", r"ai|artificial\s+intelligence|automation|automate|machine\s+learning|ml|llm|gen\s*ai|generative\s+ai"),
)
_PRICING = r"how\s+much|costs?|costing|price|prices|pricing|priced|estimate|estimates|quote|quotes|quotation|rates?|fees?|charges?|budget|expensive|cheap|affordable"
_BUYING = (
    r"need|needs|want|wanted|build|building|create|develop|make|looking\s+for|hire|get|start|launch|"
    r"redesign|revamp|require|interested\s+in|would\s+like|planning|plan\s+to|help\s+(?:me\s+)?with|set\s+up|setup|automate"
)
_INFO_QUESTION = r"what|how|do\s+you|can\s+you|which|where|tell\s+me|does|why|who|is\s+it|are\s+you|explain"
_GENERIC_PROJECT = (
    r"(?:can|could|would)\s+you\s+(?:build|develop|create|make|work\s+on)\s+(?:this|something|for\s+us|for\s+me)",
    r"(?:we|i)\s+(?:want|need|looking)\s+to\s+(?:hire|work\s+with)\s+you",
    r"can\s+you\s+(?:help\s+us\s+)?build\s+this",
    r"build\s+this\s+for\s+us",
)


def detect_service(message: str) -> str | None:
    text = normalize(message)
    for key, pattern in _SERVICE_PATTERNS:
        if _has(pattern, text):
            return key
    return None


def is_pricing_question(message: str) -> bool:
    return _has(_PRICING, normalize(message))


def is_question(message: str) -> bool:
    text = normalize(message)
    return "?" in (message or "") or re.match(rf"(?:{_INFO_QUESTION})(?!\w)", text) is not None


def classify_intent_deterministic(message: str) -> IntentClassificationResult | None:
    """High-precision keyword routing. Returns None when nothing matches confidently."""
    text = normalize(message)
    if not text:
        return None

    cmd = detect_global_command(message)
    if cmd:
        return IntentClassificationResult(intent=cmd, confidence=1.0)

    if is_acknowledgement(message):
        return IntentClassificationResult(intent="ACKNOWLEDGEMENT", confidence=0.98)

    chitchat = detect_chitchat(message)
    if chitchat:
        intent_name = "HESITATION" if chitchat["type"] == "hesitation" else "CHITCHAT"
        return IntentClassificationResult(intent=intent_name, confidence=0.95)

    handoff = detect_handoff_command(message)
    if handoff:
        return IntentClassificationResult(intent=handoff, confidence=0.98, is_buying_intent=True)

    service = detect_service(message)

    if is_pricing_question(message):
        return IntentClassificationResult(
            intent="PRICING", confidence=0.95, is_buying_intent=True,
            service_key=service, extracted_requirement=message.strip(),
        )

    if _has(r"portfolio|previous\s+work|past\s+work|case\s+stud(?:y|ies)|examples?\s+of\s+(?:your\s+)?work|your\s+work|clients", text):
        return IntentClassificationResult(intent="PORTFOLIO", confidence=0.92)

    if _has(r"services|what\s+do\s+you\s+(?:do|offer)|what\s+can\s+you\s+do|offerings", text) and not service:
        return IntentClassificationResult(intent="SERVICE_INQUIRY", confidence=0.9)

    if service:
        if is_question(message) and not _has(_BUYING, text):
            return None
        buying = _has(_BUYING, text) and not re.match(rf"(?:{_INFO_QUESTION})(?!\w)", text)
        return IntentClassificationResult(
            intent=service, confidence=0.9, is_buying_intent=buying,
            service_key=service, extracted_requirement=message.strip(),
        )

    if any(_has(p, text) for p in _GENERIC_PROJECT) and not service:
        return IntentClassificationResult(
            intent="GENERIC_REQUIREMENT",
            confidence=0.9,
            is_buying_intent=True,
            service_key=None,
            extracted_requirement=message.strip(),
        )

    return None


def classify_global_intent(message: str) -> IntentClassificationResult:
    """
    Classifies the user message into high-level intents.
    Deterministic rules run first; Gemini is the fallback for free text.
    """
    result = classify_intent_deterministic(message)
    if result:
        return result

    prompt = f"""
Analyze the user message in the context of a software, web, mobile, AI and digital growth agency's sales assistant.

Message: "{message}"

Classify into exactly one intent:
COMPANY_INFO, SERVICE_INQUIRY, WEBSITE, MOBILE_APP, CUSTOM_SOFTWARE, AI_AUTOMATION, CHATBOT,
SHOPIFY_ECOMMERCE, SEO, PORTFOLIO, PRICING, BOOK_CALL, HUMAN_AGENT, CONTACT_ME, MAIN_MENU,
ACKNOWLEDGEMENT, CHITCHAT, HESITATION, OTHER

Fields:
- intent: one value from the list above.
- confidence: float between 0 and 1.
- is_buying_intent: true only if the user wants to build, buy, hire, get a quote, or start a project.
- service_key: one of WEBSITE, MOBILE_APP, CUSTOM_SOFTWARE, AI_AUTOMATION, CHATBOT, SHOPIFY_ECOMMERCE, SEO if the
  message is about a specific service, else null.
- extracted_requirement: short summary of what they want, or null.
Use OTHER with low confidence when unsure.
"""
    text = llm.generate(
        client, prompt, "classify_global_intent",
        types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=IntentClassificationResult,
            temperature=0.1,
        ),
    )
    if not text:
        return IntentClassificationResult(intent="OTHER", confidence=0.0)
    try:
        result = IntentClassificationResult.model_validate_json(text)
    except Exception as e:
        logger.warning("Gemini intent result was not valid JSON: %s", e)
        return IntentClassificationResult(intent="OTHER", confidence=0.0)
    if result.service_key not in SERVICE_INTENTS:
        result.service_key = result.intent if result.intent in SERVICE_INTENTS else None
    return result


# ----------------------------------------------------------------------
# Option matching
# ----------------------------------------------------------------------
def option_id(label: str) -> str:
    """Generates a slugified option ID from a label."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def match_option_exact(options: dict[str, Any] | list[str], message: str) -> str | None:
    """Exact label / slug / alias match (case-insensitive, punctuation-insensitive)."""
    if not options or not message:
        return None
    clean = message.strip().lower()
    norm = normalize(message)
    for label in options:
        if label.lower() == clean or option_id(label) == clean or normalize(label) == norm:
            return label
    if isinstance(options, dict):
        for label, cfg in options.items():
            aliases = cfg.get("aliases", []) if isinstance(cfg, dict) else []
            if any(normalize(a) == norm for a in aliases):
                return label
    return None


def match_option_fuzzy(options: dict[str, Any] | list[str], message: str) -> str | None:
    """
    Word-boundary containment match. Returns a label only when exactly one option
    matches, so ambiguous text is never routed by guesswork.
    """
    exact = match_option_exact(options, message)
    if exact:
        return exact
    norm = normalize(message)
    if not norm:
        return None

    candidates: dict[str, int] = {}
    for label in options:
        label_norm = normalize(label)
        if not label_norm:
            continue
        phrases = [label_norm]
        if isinstance(options, dict) and isinstance(options[label], dict):
            phrases += [normalize(a) for a in options[label].get("aliases", [])]
        for phrase in phrases:
            if not phrase:
                continue
            # The option (or an alias) appears inside the message ...
            if _has(re.escape(phrase), norm):
                candidates[label] = max(candidates.get(label, 0), len(phrase))
            # ... or a short message is part of an option label ("1 month" -> "Within 1 Month").
            elif len(norm) >= 4 and len(norm.split()) <= 4 and _has(re.escape(norm), phrase):
                candidates[label] = max(candidates.get(label, 0), len(norm))

    if not candidates:
        return None
    best = max(candidates.values())
    winners = [label for label, score in candidates.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def match_option(message: str, options: list[str]) -> OptionMatchResult:
    """Matches a user message against a list of available options (deterministic, then Gemini)."""
    if not options:
        return OptionMatchResult(selected_option="UNKNOWN", confidence=0.0)

    exact = match_option_exact(options, message)
    if exact:
        return OptionMatchResult(selected_option=exact, confidence=1.0)

    fuzzy = match_option_fuzzy(options, message)
    if fuzzy:
        return OptionMatchResult(selected_option=fuzzy, confidence=0.9)

    prompt = f"""
You are routing a user inside a lead-generation conversation.

User message:
"{message}"

Available options:
{options}

Select the option that best matches the user's meaning.
Rules:
- selected_option must exactly match one of the available options.
- If none clearly match, return UNKNOWN.
- confidence must be between 0.0 and 1.0.
"""
    result = _generate_match_result(prompt, "match_option")
    if result.selected_option not in options and result.selected_option != "UNKNOWN":
        return OptionMatchResult(selected_option="UNKNOWN", confidence=0.0)
    return result


def match_global_command(message: str) -> str | None:
    """
    Gemini fallback: is this message asking to return to the start / restart?
    """
    prompt = f"""
Classify whether this user message is asking to return to the start of the
conversation or restart it.

User message:
"{message}"

Select exactly one:
- MAIN_MENU: asks to see the main menu, go home, go back to the beginning,
  or return to the start.
- START: a greeting that should start or restart the conversation.
- NONE: anything else, including requests to select a service or speak to a
  person.

Only select MAIN_MENU or START when the intent is clear. Otherwise select NONE.
confidence must be between 0 and 1.
"""
    result = _generate_match_result(prompt, "match_global_command")

    if result.selected_option not in {
        "MAIN_MENU",
        "START",
        "NONE",
    } or result.confidence < 0.75:
        return "NONE"

    return (
        None
        if result.selected_option == "NONE"
        else result.selected_option
    )


def _generate_match_result(prompt: str, operation: str) -> OptionMatchResult:
    """Helper to query Gemini with OptionMatchResult schema."""
    text = llm.generate(
        client, prompt, operation,
        types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=OptionMatchResult,
            temperature=0.1,
        ),
    )
    if not text:
        return OptionMatchResult(selected_option="UNKNOWN", confidence=0.0)
    try:
        return OptionMatchResult.model_validate_json(text)
    except Exception as e:
        logger.warning("%s returned invalid JSON: %s", operation, e)
        return OptionMatchResult(selected_option="UNKNOWN", confidence=0.0)
