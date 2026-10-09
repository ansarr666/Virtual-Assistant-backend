# Omnichannel Conversational Lead Bot

FastAPI backend + React/Vite web chat for a multi-tenant lead-generation assistant
(OrionSoft Technologies is tenant `client_001`). The same conversation flow runs on the
**website, Facebook Messenger, Instagram DM and WhatsApp**. Conversation state and transcripts
are persisted to Google Sheets; Gemini is an optional enhancement for free-text understanding.

```
flows/orionsoft_flow.json  ->  orchestrator  ->  {reply, options}  ->  channel renderer
                                                                       web:       buttons
                                                                       Messenger: quick replies
                                                                       Instagram: quick replies
                                                                       WhatsApp:  reply buttons / list
```

## Run locally

Ports are fixed: **backend 8001**, **frontend 5173**.

```powershell
# Backend (from the project root, using the project venv)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001

# Frontend (second terminal)
cd frontend
npm install
npm run dev          # http://localhost:5173 -> calls http://127.0.0.1:8001/chat
```

The frontend's backend URL lives in one place: `frontend/.env.development`
(`VITE_API_BASE_URL=http://127.0.0.1:8001`). Restart `npm run dev` after changing it.

Before starting, make sure no old dev servers are still running (`netstat -ano | findstr ":8000 :8001 :5173"`).
A stale Vite or uvicorn process on the same port silently receives requests meant for the new one.

`.env` is read once at startup: **restart uvicorn after editing it** (`--reload` only watches `.py` files).

## Configuration (`.env`)

All settings are listed in `.env.example`.

| Variable | Purpose |
|---|---|
| `GOOGLE_SHEET_ID` | Spreadsheet with `conversations`, `messages`, `conversation_state`, `users` worksheets. Missing columns are added to the header row automatically. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account key as a file path, or as JSON text / base64 (for servers). If both are empty, Application Default Credentials are used. |
| `REPOSITORY_BACKEND` | `google_sheets` (default) or `memory` for local work without Google access. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Optional. Without them (or when the quota is exhausted) routing is fully deterministic. |
| `GEMINI_TIMEOUT_SECONDS`, `GEMINI_COOLDOWN_SECONDS` | Per-call timeout (minimum 10 s, Gemini's limit) and how long to skip Gemini after a quota/outage error. |
| `CORS_ORIGINS` | Comma-separated browser origins. Default: `http://localhost:5173,http://127.0.0.1:5173`. |
| `LOG_LEVEL` | Default `INFO`: logs every request lifecycle with timings. |
| `META_VERIFY_TOKEN` | Any random string; type the same value into the Meta webhook settings. |
| `META_APP_ID`, `META_APP_SECRET` | Meta app (App settings > Basic). The secret verifies webhook signatures. |
| `META_API_VERSION` | Graph API version, e.g. `v20.0`. |
| `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN` | Facebook Page and a **Page** access token (not a user token). |
| `IG_ACCOUNT_ID`, `IG_ACCESS_TOKEN` | Instagram business account (`/me` `user_id`, starts with `1784…`) and its Instagram Login token. |
| `IG_APP_ID`, `IG_APP_SECRET` | Instagram app ID/secret (Instagram > API setup with Instagram login). The secret verifies Instagram webhook signatures. |
| `IG_TOKEN_AUTO_REFRESH` | Default `true`: refreshes the ~60-day Instagram token automatically. |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN` | WhatsApp Cloud API number ID and a never-expiring system-user token. |

### Google credentials

Locally, Sheets access can use ADC with the `https://www.googleapis.com/auth/spreadsheets` scope:

```powershell
gcloud auth application-default login --impersonate-service-account=<sa>@<project>.iam.gserviceaccount.com
```

On a server, use `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the key JSON, or its base64).
The spreadsheet must be shared with the service account. Nothing authenticates at import
time: the API always starts, warms up Sheets in the background, and `/chat` returns a
controlled `503` (with the reason in the server log) if Sheets is unreachable.

## API

`POST /chat`

```json
{"tenant_id": "client_001", "user_id": "user-1", "message": "Hi", "channel": "web",
 "client_message_id": "8b1d6c1e-...-uuid"}
```

Response: `reply`, `options`, `mode`, `intent`, `conversation_status`, `current_step`.
Retries must reuse the same `client_message_id`; the stored response is returned and
state is not advanced twice.

`GET /` health, `GET /health` configuration/flow/channel status (booleans only, no Google calls).

### Messaging webhooks

| Route | Used by |
|---|---|
| `GET/POST /webhooks/meta` | Facebook Page, Instagram and WhatsApp (one callback for all) |
| `GET/POST /webhooks/facebook`, `/webhooks/instagram` | Aliases of `/webhooks/meta` |
| `GET/POST /webhooks/whatsapp` | WhatsApp (same parser) |

- **Signatures:** every POST must carry Meta's `X-Hub-Signature-256` (HMAC of the body with
  `META_APP_SECRET`, or `IG_APP_SECRET` for Instagram). Unsigned requests are rejected with 403,
  except requests made directly on the same machine (no proxy headers) for local testing.
- Meta gets `200` immediately; the message is processed in the background. Redelivered
  messages (same message id) are ignored, and the bot never answers its own echoes.
- Options are sent natively with stable IDs (`option_id()` slugs, e.g. `our-services`); taps
  come back as `quick_reply.payload` / `postback.payload` / WhatsApp `button_reply.id` /
  `list_reply.id`. Long labels use the flow's `short_labels` on Messenger/Instagram/WhatsApp.
- Photos, voice notes, files and locations get a short "I can only read text" reply;
  stickers, likes and reactions are ignored.
- WhatsApp phone numbers are masked (`***1234`) in channel logs.

## Connecting the channels in Meta

Run the checker after any change (it never prints secrets):

```powershell
.venv\Scripts\python.exe scripts\check_channels.py           # read-only
.venv\Scripts\python.exe scripts\check_channels.py --fix     # re-subscribe Page / Instagram / WhatsApp
.venv\Scripts\python.exe scripts\check_channels.py --url https://your-server.example.com
```

1. **App mode:** real messages are only delivered while the Meta app is **Live** (published).
   Until Advanced Access is approved (App Review), only people with a role on the app
   (admins, developers, testers) get replies.
2. **Facebook:** Page token in `FB_PAGE_ACCESS_TOKEN`; Webhooks > Page: callback
   `https://<host>/webhooks/meta`, fields `messages`, `messaging_postbacks`; Page subscribed
   (`check_channels.py --fix`).
3. **Instagram:** token generated for the business account (`/me` username must be the business);
   `IG_ACCOUNT_ID` = its `user_id`; Instagram webhooks callback `https://<host>/webhooks/meta`,
   field `messages`; `IG_APP_SECRET` set.
4. **WhatsApp:** system-user token (expiration "Never", `whatsapp_business_messaging` +
   `whatsapp_business_management`); WhatsApp > Configuration: callback
   `https://<host>/webhooks/meta`, field `messages`; WhatsApp Business account subscribed
   (`check_channels.py --fix`).

## Deployment (always-on server)

Meta **disables webhooks after 1 hour of failed deliveries**, so production must run on an
always-on server, not a laptop + ngrok.

- `Dockerfile`: runs one uvicorn worker on `$PORT` (default 8000) as a non-root user, with a
  health check on `/health`. **Keep exactly one worker/instance**: message de-duplication, menu
  paging and per-conversation locks live in memory.
- `render.yaml`: Render Blueprint (Docker, paid "starter" instance — free instances sleep and
  Meta would disable the webhooks). Fill in the secret environment variables in Render.
- After deploying: point all Meta callbacks to `https://<host>/webhooks/meta`, run
  `scripts/check_channels.py --url https://<host> --fix`, and set the frontend's
  `VITE_API_BASE_URL` (and the backend's `CORS_ORIGINS`) to the production URLs.
- `docker compose up -d` runs the same image locally with `.env`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Messages reach the Page/Inbox but nothing reaches the server | Meta stopped delivering (app not Live, or "Webhooks Disabled" after an outage). Run `check_channels.py --fix`; check the app is Live. |
| `code 190` / "Session has expired" | Token expired: generate a never-expiring one (Page token from a long-lived user token; WhatsApp system-user token). |
| `#210 A page access token is required` | `FB_PAGE_ACCESS_TOKEN` holds a user token; put the Page token there. |
| Instagram `code 100 / subcode 2534014` | Recipient not found (e.g. Meta's fake test user). |
| WhatsApp `#131030 not in allowed list` | Test number: add the recipient under WhatsApp > API Setup, or use a production number. |
| Webhook POSTs get `403 missing_signature` / `invalid_signature` | Request not signed by Meta, or `META_APP_SECRET` / `IG_APP_SECRET` is wrong. |

## Conversation flows

Flows live in `flows/*.json` and are validated at startup and on load (broken references
never reach users). Validate manually with:

```powershell
python -m app.services.flow_service
```

Besides `steps`, a flow declares `routes` (where handoff / pricing / RAG / main-menu intents
go), `global_options` (buttons that work from any step), `fallback_options`, a `services`
catalog used for context-aware pricing and requirement steps, and `short_labels` (display
labels of at most 20 characters for Messenger/Instagram/WhatsApp buttons).

## Tests

```powershell
pytest
```

Tests never call Gemini, Google Sheets or Meta (see `tests/conftest.py`).
