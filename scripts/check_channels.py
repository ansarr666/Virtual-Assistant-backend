"""
Checks the Meta connection of all three channels (Facebook Messenger, Instagram, WhatsApp).
Never prints tokens or secrets.

Run from the project root:
    .venv\\Scripts\\python.exe scripts\\check_channels.py            read-only checks
    .venv\\Scripts\\python.exe scripts\\check_channels.py --fix      also (re)subscribes this app to the
                                                                  Facebook Page, Instagram account and
                                                                  WhatsApp Business account
    add --url https://your-server.example.com to compare the webhook callbacks with a deployed URL
    (otherwise the running ngrok URL is used).

--fix is the recovery Meta documents after a "Webhooks Disabled" event: subscribe again.
It only touches THIS app's subscriptions.
"""
import datetime
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import ENV_FILE, get_settings  # noqa: E402

FIX = "--fix" in sys.argv
s = get_settings()
env = dotenv_values(ENV_FILE)
APP_ID = (env.get("META_APP_ID") or "").strip()
IG_APP_ID = (env.get("IG_APP_ID") or "").strip()
FB = f"{s.META_GRAPH_API_URL.rstrip('/')}/{s.META_API_VERSION}"
IG = f"{s.IG_GRAPH_API_URL.rstrip('/')}/{s.META_API_VERSION}"
APP_TOKEN = f"{APP_ID}|{s.META_APP_SECRET}"
MESSAGING_FIELDS = "messages,messaging_postbacks"
rows: list[tuple[str, str, str, str]] = []


def add(channel: str, name: str, ok: bool | None, detail: str = "") -> None:
    rows.append((channel, name, "PASS" if ok else ("INFO" if ok is None else "FAIL"), detail))


def call(method: str, url: str, token: str, **params):
    try:
        r = httpx.request(method, url, params={**params, "access_token": token}, timeout=20)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    except Exception as e:
        return 0, {"error": {"message": type(e).__name__}}


def err(d: dict) -> str:
    e = (d or {}).get("error") or {}
    return f"{e.get('code')} {e.get('message')}" if e else ""


def when(ts) -> str:
    if ts == 0:
        return "never"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"


def debug(token: str) -> dict:
    _, d = call("GET", f"{FB}/debug_token", APP_TOKEN, input_token=token)
    return d.get("data") or {}


# ------------------------------------------------------------------ public URL
url_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--url=")), "")
if not url_arg and "--url" in sys.argv:
    i = sys.argv.index("--url")
    url_arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
public = url_arg.rstrip("/")
if not public:
    try:
        tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=5).json()["tunnels"]
        public = next((t["public_url"] for t in tunnels if t["public_url"].startswith("https")), "")
    except Exception:
        public = ""
add("all", "public URL", bool(public), public or "no --url given and ngrok not running")
add("all", "META_APP_ID / META_APP_SECRET", bool(APP_ID and s.META_APP_SECRET), APP_ID)
add("all", "app mode", None, "Real messages need the app to be Live (Meta dashboard). Not checkable via API.")

_, subs = call("GET", f"{FB}/{APP_ID}/subscriptions", APP_TOKEN)
app_subs = {x.get("object"): x for x in subs.get("data", [])}


def app_sub_check(channel: str, obj: str, required: list[str]) -> None:
    sub = app_subs.get(obj)
    if not sub:
        add(channel, f"app webhook '{obj}'", False, err(subs) or "not configured in the Meta dashboard")
        return
    names = [f.get("name") for f in sub.get("fields", [])]
    cb = sub.get("callback_url", "")
    add(channel, f"app webhook '{obj}' active", bool(sub.get("active")), cb)
    add(channel, "callback matches public URL", bool(public) and cb.startswith(public), cb)
    missing = [f for f in required if f not in names]
    add(channel, "webhook fields", not missing, f"{names}" + (f" missing {missing}" if missing else ""))


# ------------------------------------------------------------------ Facebook
page_token = s.FB_PAGE_ACCESS_TOKEN
if not page_token:
    add("facebook", "FB_PAGE_ACCESS_TOKEN", False, "empty")
else:
    info = debug(page_token)
    if info.get("type") == "USER":
        _, d = call("GET", f"{FB}/{s.FB_PAGE_ID}", page_token, fields="access_token")
        add("facebook", "token type", False,
            "FB_PAGE_ACCESS_TOKEN is a USER token - put the PAGE token in .env "
            "(Graph API Explorer: <page_id>?fields=access_token). Using a derived Page token for this run.")
        page_token = d.get("access_token") or page_token
        info = debug(page_token)
    add("facebook", "page token valid", bool(info.get("is_valid")),
        f"type={info.get('type')} expires={when(info.get('expires_at'))}")
    add("facebook", "pages_messaging permission", "pages_messaging" in (info.get("scopes") or []))
    _, me = call("GET", f"{FB}/me", page_token, fields="id,name")
    add("facebook", "token belongs to FB_PAGE_ID", me.get("id") == s.FB_PAGE_ID, f"{me.get('id')} {me.get('name') or err(me)}")

    def page_subs():
        _, d = call("GET", f"{FB}/{s.FB_PAGE_ID}/subscribed_apps", page_token, fields="id,subscribed_fields")
        return {a.get("id"): a.get("subscribed_fields") for a in d.get("data", [])}

    if FIX:
        call("DELETE", f"{FB}/{s.FB_PAGE_ID}/subscribed_apps", page_token)
        code, d = call("POST", f"{FB}/{s.FB_PAGE_ID}/subscribed_apps", page_token, subscribed_fields=MESSAGING_FIELDS)
        add("facebook", "FIX: page re-subscribed", code == 200 and d.get("success"), err(d))
    mine = page_subs().get(APP_ID)
    add("facebook", "page subscribed to this app", bool(mine), str(mine))
app_sub_check("facebook", "page", ["messages", "messaging_postbacks"])

# ------------------------------------------------------------------ Instagram
ig_token = s.IG_ACCESS_TOKEN
cache = Path(s.IG_TOKEN_CACHE_FILE)
if cache.is_file():
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        days = (float(c.get("expires_at", 0)) - time.time()) / 86400
        add("instagram", "auto-refreshed token cache", days > 7, f"expires in {days:.0f} days")
    except ValueError:
        add("instagram", "auto-refreshed token cache", False, "unreadable")
if not ig_token:
    add("instagram", "IG_ACCESS_TOKEN", False, "empty")
else:
    _, me = call("GET", f"{IG}/me", ig_token, fields="user_id,username")
    add("instagram", "token works", "user_id" in me, f"username={me.get('username')} {err(me)}")
    if "user_id" in me:
        ok = s.IG_ACCOUNT_ID == str(me["user_id"])
        add("instagram", "IG_ACCOUNT_ID correct", ok,
            "matches" if ok else f"set IG_ACCOUNT_ID={me['user_id']} in .env (currently {s.IG_ACCOUNT_ID or 'empty'})")
    if FIX:
        code, d = call("POST", f"{IG}/me/subscribed_apps", ig_token, subscribed_fields=MESSAGING_FIELDS)
        add("instagram", "FIX: account re-subscribed", code == 200 and d.get("success"), err(d))
    _, d = call("GET", f"{IG}/me/subscribed_apps", ig_token)
    fields = [f for a in d.get("data", []) for f in (a.get("subscribed_fields") or [])]
    add("instagram", "account subscribed (messages)", "messages" in fields, str(fields) or err(d))
add("instagram", "IG_APP_SECRET (webhook signatures)", bool(s.IG_APP_SECRET),
    "" if s.IG_APP_SECRET else "real Instagram webhooks will be rejected without it")
if IG_APP_ID and s.IG_APP_SECRET:
    _, d = call("GET", f"{FB}/{IG_APP_ID}/subscriptions", f"{IG_APP_ID}|{s.IG_APP_SECRET}")
    ig_sub = next((x for x in d.get("data", []) if x.get("object") == "instagram"), None)
    add("instagram", "Instagram app webhook", bool(ig_sub and ig_sub.get("active")),
        (ig_sub or {}).get("callback_url") or err(d) or "not configured")
else:
    add("instagram", "Instagram app webhook", None, "add IG_APP_ID to .env to check it (optional)")

# ------------------------------------------------------------------ WhatsApp
wa_token, phone_id = s.WHATSAPP_ACCESS_TOKEN, s.WHATSAPP_PHONE_NUMBER_ID
if not (wa_token and phone_id):
    add("whatsapp", "WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID", False, "missing")
else:
    info = debug(wa_token)
    scopes = info.get("scopes") or []
    add("whatsapp", "token valid", bool(info.get("is_valid")),
        f"type={info.get('type')} expires={when(info.get('expires_at'))}")
    add("whatsapp", "whatsapp_business_messaging permission", "whatsapp_business_messaging" in scopes)
    _, ph = call("GET", f"{FB}/{phone_id}", wa_token, fields="display_phone_number,verified_name,quality_rating")
    add("whatsapp", "phone number reachable", "display_phone_number" in ph,
        f"{ph.get('verified_name')} {ph.get('display_phone_number')} quality={ph.get('quality_rating')} {err(ph)}")
    wabas = sorted({t for g in info.get("granular_scopes") or []
                    if g.get("scope", "").startswith("whatsapp_business") for t in g.get("target_ids") or []})
    if not wabas:
        add("whatsapp", "WhatsApp Business account", None, "could not determine the account ID from the token")
    for waba in wabas:
        if FIX:
            code, d = call("POST", f"{FB}/{waba}/subscribed_apps", wa_token)
            add("whatsapp", f"FIX: account {waba} subscribed", code == 200 and d.get("success"), err(d))
        _, d = call("GET", f"{FB}/{waba}/subscribed_apps", wa_token)
        ids = [((a.get("whatsapp_business_api_data") or {}).get("id") or a.get("id")) for a in d.get("data", [])]
        add("whatsapp", f"account {waba} subscribed to this app", APP_ID in ids, str(ids) or err(d))
app_sub_check("whatsapp", "whatsapp_business_account", ["messages"])

# ------------------------------------------------------------------ report
print(f"\n{'CHANNEL':10} {'CHECK':42} RESULT  DETAIL")
print("-" * 110)
for channel, name, res, detail in rows:
    print(f"{channel:10} {name:42} {res:6}  {detail}")
fails = [r for r in rows if r[2] == "FAIL"]
print(f"\n{len(fails)} problem(s)." + ("" if FIX or not fails else "  Re-run with --fix to re-subscribe all three channels."))
