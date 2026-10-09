"""
Facebook Messenger webhook diagnostics. Never prints tokens or the app secret.

Run from the project root:
    .venv\\Scripts\\python.exe scripts\\diagnose_facebook_meta.py                 (read-only checks)
    .venv\\Scripts\\python.exe scripts\\diagnose_facebook_meta.py --resubscribe   (also refreshes the Page subscription)

--resubscribe removes ONLY this app's subscription on the Page, adds it back with
messages + messaging_postbacks, and shows the state before and after. This is the recovery Meta
documents after a "Webhooks Disabled" event ("you will need to subscribe to the Webhooks again").
"""
import datetime
import hashlib
import hmac
import json
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import ENV_FILE, get_settings  # noqa: E402

FIELDS = "messages,messaging_postbacks"
LOCAL = "http://127.0.0.1:8001"

s = get_settings()
app_id = (dotenv_values(ENV_FILE).get("META_APP_ID") or "").strip()
graph = f"{s.META_GRAPH_API_URL}/{s.META_API_VERSION}"
app_token = f"{app_id}|{s.META_APP_SECRET}"
page_token = s.FB_PAGE_ACCESS_TOKEN
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool | None, detail: str = "") -> None:
    results.append((name, "PASS" if ok else ("INFO" if ok is None else "FAIL"), detail))


def g(method: str, path: str, token: str, **params):
    try:
        r = httpx.request(method, f"{graph}/{path}", params={**params, "access_token": token}, timeout=20)
        return r.status_code, r.json()
    except Exception as e:  # network
        return 0, {"error": {"message": type(e).__name__}}


def err(d: dict) -> str:
    e = d.get("error") or {}
    return f"{e.get('code')} {e.get('message')}" if e else ""


def page_subscriptions() -> list:
    _, d = g("GET", f"{s.FB_PAGE_ID}/subscribed_apps", page_token, fields="id,name,subscribed_fields")
    return [(a.get("id"), a.get("subscribed_fields")) for a in d.get("data", [])]


# ---------------------------------------------------------------- config (booleans only)
for name in ("META_VERIFY_TOKEN", "META_APP_SECRET", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"):
    check(f"config {name} present", bool(getattr(s, name)))
check("config META_APP_ID present", bool(app_id), app_id)
check("Graph API version", None, s.META_API_VERSION)

# ---------------------------------------------------------------- tunnel + routes
public = ""
try:
    tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=5).json()["tunnels"]
    public = next((t["public_url"] for t in tunnels if t["public_url"].startswith("https")), "")
    check("ngrok online", bool(public), public)
except Exception:
    check("ngrok online", False, "ngrok API not reachable on 127.0.0.1:4040")

for base_url, label in ((LOCAL, "local"), (public, "public")):
    if not base_url:
        continue
    for path in ("/webhooks/meta", "/webhooks/facebook"):
        try:
            r = httpx.get(base_url + path, params={"hub.mode": "subscribe", "hub.verify_token": s.META_VERIFY_TOKEN,
                                                   "hub.challenge": "12345"}, timeout=15)
            check(f"{label} GET verify {path}", r.status_code == 200 and r.text == "12345", f"HTTP {r.status_code}")
        except Exception as e:
            check(f"{label} GET verify {path}", False, type(e).__name__)

if public:
    body = json.dumps({"object": "page", "entry": []}).encode()
    sig = "sha256=" + hmac.new(s.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    for path in ("/webhooks/meta", "/webhooks/facebook"):
        r = httpx.post(public + path, content=body, timeout=15,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
        check(f"public signed POST {path}", r.status_code == 200, f"HTTP {r.status_code} {r.text[:40]}")

# ---------------------------------------------------------------- Graph API state
# If FB_PAGE_ACCESS_TOKEN holds a USER token, derive the Page token from it (kept in memory only).
_, d = g("GET", "debug_token", app_token, input_token=page_token)
if (d.get("data") or {}).get("type") == "USER":
    _, d = g("GET", s.FB_PAGE_ID, page_token, fields="access_token")
    derived = d.get("access_token")
    check("FB_PAGE_ACCESS_TOKEN is a USER token", False,
          "derived the Page token from it for this run" if derived else f"could not derive Page token: {err(d)}")
    if derived:
        page_token = derived

code, d = g("GET", "me", page_token, fields="id,name")
check("Page token identity", code == 200 and d.get("id") == s.FB_PAGE_ID, f"{d.get('id')} {d.get('name') or err(d)}")

code, d = g("GET", "debug_token", app_token, input_token=page_token)
data = d.get("data", {})
exp = data.get("expires_at")
exp_txt = "never" if exp == 0 else (datetime.datetime.fromtimestamp(exp).isoformat() if exp else str(exp))
check("Page token valid", bool(data.get("is_valid")),
      f"type={data.get('type')} app={data.get('app_id')} expires={exp_txt}")
scopes = data.get("scopes") or []
check("scope pages_messaging", "pages_messaging" in scopes, ",".join(scopes))
check("scope pages_manage_metadata", "pages_manage_metadata" in scopes)

subs = page_subscriptions()
mine = [f for (i, f) in subs if i == app_id]
check("Page -> App subscription", bool(mine), str(subs))
check("Page field messages", bool(mine) and "messages" in mine[0])
check("Page field messaging_postbacks", bool(mine) and "messaging_postbacks" in mine[0])

code, d = g("GET", f"{app_id}/subscriptions", app_token)
page_sub = next((x for x in d.get("data", []) if x.get("object") == "page"), None)
if page_sub:
    names = [f.get("name") for f in page_sub.get("fields", [])]
    cb = page_sub.get("callback_url", "")
    check("App page webhook active", bool(page_sub.get("active")), cb)
    check("App callback matches ngrok", bool(public) and cb.startswith(public), f"callback={cb}")
    check("App field messages", "messages" in names, str(names))
else:
    check("App page webhook active", False, err(d) or "no 'page' subscription")

# ---------------------------------------------------------------- optional recovery
if "--resubscribe" in sys.argv:
    before = page_subscriptions()
    code_d, d_del = g("DELETE", f"{s.FB_PAGE_ID}/subscribed_apps", page_token)
    code_p, d_post = g("POST", f"{s.FB_PAGE_ID}/subscribed_apps", page_token, subscribed_fields=FIELDS)
    after = page_subscriptions()
    check("RESUBSCRIBE delete", code_d == 200 and d_del.get("success"), err(d_del))
    check("RESUBSCRIBE add", code_p == 200 and d_post.get("success"), err(d_post))
    check("RESUBSCRIBE before -> after", any(i == app_id for i, _ in after), f"{before} -> {after}")

# ---------------------------------------------------------------- report
print(f"\n{'CHECK':38} RESULT  DETAIL")
print("-" * 100)
for name, res, detail in results:
    print(f"{name:38} {res:6}  {detail}")
print("\nMeta-generated delivery cannot be tested from here: send a real Messenger message and check ngrok "
      "(http://127.0.0.1:4040) for a POST with User-Agent facebookplatform.")
