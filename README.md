# Omnichannel Conversational Lead Bot (Backend)

FastAPI backend for a multi-tenant lead-generation assistant
(OrionSoft Technologies is tenant `client_001`). The same conversation flow runs on the
**Facebook Messenger, Instagram DM and WhatsApp**. Conversation state and transcripts
are persisted to Google Sheets; Gemini & OpenRouter provide resilient free-text understanding and FAQ routing.

```
flows/orionsoft_flow.json  ->  orchestrator  ->  {reply, options}  ->  channel renderer
                                                                       Messenger: quick replies
                                                                       Instagram: quick replies
                                                                       WhatsApp:  reply buttons / list
```

## Run locally

Backend default port: **8001** (web frontend connects via `CORS_ORIGINS`).

```powershell
# Backend (from the project root, using the project venv)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

> [!NOTE]
> The Web channel and `/chat` endpoint have been decommissioned. Inbound messaging is handled via Meta and WhatsApp Cloud webhooks.

Before starting, make sure no old dev servers are still running (`netstat -ano | findstr ":8000 :8001"`).
A stale uvicorn process on the same port silently receives requests meant for the new one.

`.env` is read once at startup: **restart uvicorn after editing it** (`--reload` only watches `.py` files).

## Core Features & Enterprise Hardening

### 1. Guardrails & Role Adherence
- **Strict Role Boundaries:** Automatically detects and deflects off-topic queries (coding tasks, software debugging, creative poetry, trivia, or prompt injections).
- **Polite Steering:** Gracefully informs users of the bot's focus (custom software, AI engineering, consultation) and guides them back to the active conversation flow.
- **Strict Field Validation:** Validates required fields such as RFC 5322 email syntax and international phone numbers during contact capture steps before advancing state.

### 2. Returning User Lifecycle (Resume vs. Restart)
- **Data Isolation:** User ID is decoupled from Conversation ID (`conv_<user_id>_<timestamp>`). Old conversation history, state, and leads are never overwritten.
- **Session Continuity:** Returning users (after >10 minutes idle or a new session start) are prompted with a clear choice:
  - **Continue:** Resumes right where they left off in their existing flow.
  - **Start New Conversation:** Marks the prior conversation as `restarted` and begins a fresh session.
- **Explicit Restart:** Sending `"restart"`, `"start over"`, or `"start new conversation"` cleanly restarts at any point.

### 3. Multi-tier AI Fallback & Low-Latency Caching
- **Resilient AI Cascade:**
  1. **Primary AI:** Google Gemini (`gemini-3.8-flash` via `GEMINI_API_KEY`)
  2. **Secondary AI (Backup):** OpenRouter (`openai/gpt-4o-mini` via `OPENROUTER_API_KEY`)
  3. **Deterministic Fallback:** Keyword & flow-based rule engine guaranteeing 100% uptime even during external AI outages.
- **Safe Knowledge Semantic Cache:** Caches FAQ / RAG retrieval responses in-memory with TTL eviction.
  - **Zero PII Exposure:** Automatically detects and refuses to cache queries containing emails, phone numbers, personal pronouns, or sales lead qualification fields.

### 4. Pluggable Persistence: Internal Google Sheets & Client Databases
- **Internal System (OrionSoft):** Defaults to Google Sheets (`REPOSITORY_BACKEND=google_sheets`), managing dedicated worksheets for:
  - `users`: User profiles, contact details, active conversation references.
  - `conversations`: Lifecycle status, timestamps, and stages.
  - `messages`: Full omnichannel transcript log.
  - `conversation_state`: State machine progress and collected answers.
  - `leads`: Qualified sales prospects with requirements, timelines, and budgets.
- **Client Capabilities (Custom Databases):** Clients can plug in any relational or cloud database:
  - **PostgreSQL / SQLite:** Built-in `SqlLeadRepository` supporting SQLite and relational databases.
  - **Supabase:** Built-in `SupabaseLeadRepository` using PostgREST HTTP REST API.
  - **Multi-Tenant Routing:** Defined per tenant in `config/tenants/<tenant_id>.json` under `"database"` or registered programmatically with `destination_service.register_tenant_repo()`.
  - **Database Migration:** DDL schema provided in `scripts/init_client_db.sql`.

## Configuration (`.env`)

All settings are listed in `.env.example`.

| Variable | Purpose |
|---|---|
| `GOOGLE_SHEET_ID` | Spreadsheet with `conversations`, `messages`, `conversation_state`, `users`, and `leads` worksheets. Missing columns and sheets are created automatically. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account key as a file path, or as JSON text / base64 (for servers). |
| `REPOSITORY_BACKEND` | `google_sheets` (default) or `memory` for local work without Google access. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Primary AI provider for natural language understanding and FAQs. |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | Secondary backup AI provider (e.g. `openai/gpt-4o-mini`). |
| `OPENROUTER_BASE_URL`, `OPENROUTER_TIMEOUT_SECONDS` | OpenRouter endpoint and per-call timeout. |
| `SUPABASE_URL`, `SUPABASE_KEY` | Optional Supabase credentials for client database integrations. |
| `CORS_ORIGINS` | Comma-separated browser origins. Default: `http://localhost:5173,http://127.0.0.1:5173`. |
| `LOG_LEVEL` | Default `INFO`: logs every request lifecycle with timings. |
| `META_VERIFY_TOKEN` | Any random string; type the same value into the Meta webhook settings. |
| `META_APP_ID`, `META_APP_SECRET` | Meta app (App settings > Basic). The secret verifies webhook signatures. |
| `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN` | Facebook Page and a **Page** access token. |
| `IG_ACCOUNT_ID`, `IG_ACCESS_TOKEN` | Instagram business account and Instagram Login token. |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN` | WhatsApp Cloud API number ID and system-user token. |

## API

`POST /chat`

```json
{
  "tenant_id": "client_001",
  "user_id": "user-1",
  "message": "Hi",
  "channel": "web",
  "client_message_id": "8b1d6c1e-...-uuid"
}
```

Response: `reply`, `options`, `mode`, `intent`, `conversation_status`, `current_step`.
Retries must reuse the same `client_message_id`; the stored response is returned and state is not advanced twice.

`GET /` health, `GET /health` configuration/flow/channel status (booleans only, no Google calls).

### Messaging webhooks

| Route | Used by |
|---|---|
| `GET/POST /webhooks/meta` | Facebook Page, Instagram and WhatsApp (one callback for all) |
| `GET/POST /webhooks/facebook`, `/webhooks/instagram` | Aliases of `/webhooks/meta` |
| `GET/POST /webhooks/whatsapp` | WhatsApp (same parser) |

- **Signatures:** every POST must carry Meta's `X-Hub-Signature-256` (HMAC of the body with `META_APP_SECRET`, or `IG_APP_SECRET` for Instagram). Unsigned requests are rejected with 403, except requests made directly on the same machine (no proxy headers) for local testing.
- Meta gets `200` immediately; the message is processed in the background. Redelivered messages (same message id) are ignored, and the bot never answers its own echoes.
- Options are sent natively with stable IDs (`option_id()` slugs, e.g. `our-services`). Long labels use the flow's `short_labels` on Messenger/Instagram/WhatsApp.
- Photos, voice notes, files and locations get a short "I can only read text" reply; stickers, likes and reactions are ignored.
- WhatsApp phone numbers are masked (`***1234`) in channel logs.

## Connecting the channels in Meta

Run the checker after any change (it never prints secrets):

```powershell
.venv\Scripts\python.exe scripts\check_channels.py           # read-only
.venv\Scripts\python.exe scripts\check_channels.py --fix     # re-subscribe Page / Instagram / WhatsApp
.venv\Scripts\python.exe scripts\check_channels.py --url https://your-server.example.com
```

1. **App mode:** real messages are only delivered while the Meta app is **Live** (published).
2. **Facebook:** Page token in `FB_PAGE_ACCESS_TOKEN`; Webhooks > Page: callback `https://<host>/webhooks/meta`, fields `messages`, `messaging_postbacks`.
3. **Instagram:** token generated for the business account (`IG_ACCOUNT_ID` = its `user_id`); Instagram webhooks callback `https://<host>/webhooks/meta`, field `messages`.
4. **WhatsApp:** system-user token (`whatsapp_business_messaging` + `whatsapp_business_management`); WhatsApp > Configuration: callback `https://<host>/webhooks/meta`, field `messages`.

## Deployment (always-on server)

Meta **disables webhooks after 1 hour of failed deliveries**, so production must run on an always-on server, not a laptop + ngrok.

- `Dockerfile`: runs one uvicorn worker on `$PORT` (default 8000) as a non-root user, with a health check on `/health`. Message de-duplication, menu paging and per-conversation locks live in memory.
- `render.yaml`: Render Blueprint (Docker).
- After deploying: point all Meta callbacks to `https://<host>/webhooks/meta`, run `scripts/check_channels.py --url https://<host> --fix`, and set the frontend's `VITE_API_BASE_URL` (and backend's `CORS_ORIGINS`) to production URLs.

## Conversation flows

Flows live in `flows/*.json` and are validated at startup and on load. Validate manually with:

```powershell
python -m app.services.flow_service
```

## Tests

```powershell
pytest
```

Tests never call Gemini, Google Sheets, or Meta in live network requests (all external calls are mocked in unit and regression suites).
