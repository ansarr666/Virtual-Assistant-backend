"""
One-click Google Sheets Authorization Script.
Prints the direct clickable auth URL and starts the local receiver on port 8085.
"""
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / ".runtime"
TOKEN_FILE = RUNTIME_DIR / "google_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
]

CLIENT_CONFIG = {
    "installed": {
        "client_id": "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com",
        "client_secret": "d-FL95Q19q7MQmFpd7hHD0Ty",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def authenticate():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = "http://localhost:8085/"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    print("\n--- GOOGLE_AUTH_URL ---", flush=True)
    print(auth_url, flush=True)
    print("--- END_AUTH_URL ---\n", flush=True)

    # Now run the local server to catch the redirect callback
    creds = flow.run_local_server(
        port=8085,
        open_browser=False,
        authorization_prompt_message="Waiting for authorization...",
        success_message="The authentication flow has completed! You may close this window.",
    )

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n[OK] Authorized successfully! Token saved to: {TOKEN_FILE}", flush=True)
    print("Testing connection to Google Sheets...", flush=True)

    import gspread
    client = gspread.authorize(creds)
    from app.config import get_settings
    sheet_id = get_settings().GOOGLE_SHEET_ID
    sheet = client.open_by_key(sheet_id)
    print(f"[SUCCESS] Connected to Google Spreadsheet: '{sheet.title}'!", flush=True)


if __name__ == "__main__":
    authenticate()
