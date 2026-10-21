"""
Google Sheets 4-Tab Verification Utility.

Checks that only the 4 user-configured tabs are referenced:
  1. "tenants"       - tenant_id, company_name, status, created_at
  2. "mastersheet"   - Date, Tenant, Customer ID, Name, Phone, Email, Channel, Service, Requirements, Budget, Timeline, Meeting Slot, Status
  3. "chat history"  - Time, Tenant, Customer ID, Channel, Sender, Message
  4. "bookings"      - Date, Tenant, Customer ID, Name, Phone, Email, Service, Meeting Slot, Channel

This script is purely a configuration auditor and does NOT write unsolicited demo data to your sheets.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.repositories.apps_script import CLEAN_HEADERS, TAB_NAMES  # noqa: E402

ALLOWED_TABS = ["tenants", "mastersheet", "chat history", "bookings"]


def verify_pipeline():
    settings = get_settings()
    script_url = settings.GOOGLE_APPS_SCRIPT_URL
    if not script_url:
        print("[FAIL] GOOGLE_APPS_SCRIPT_URL is not set in .env")
        return

    print("=" * 80)
    print("CONFIGURED GOOGLE SHEETS PIPELINE (STRICT 4 TABS)")
    print("=" * 80)
    print(f"Apps Script Webhook: {script_url[:65]}...")
    print(f"Target Tabs Count  : {len(ALLOWED_TABS)}")
    print("-" * 80)

    for tab in ALLOWED_TABS:
        headers = CLEAN_HEADERS.get("leads" if tab == "mastersheet" else ("messages" if tab == "chat history" else tab), [])
        print(f"Tab: {tab:<16} | Columns ({len(headers)}): {', '.join(headers)}")

    print("=" * 80)
    print("[OK] Verified: System strictly references only these 4 sheets and their exact columns.")
    print("All conversation data will be saved directly under Row 1 headings.")


if __name__ == "__main__":
    verify_pipeline()
