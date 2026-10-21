from app.repositories.google_sheet import GoogleSheetRepository
from app.services.sheet_service import SheetService


class DummyWorksheet:
    def __init__(self, name):
        self.name = name


class DummySpreadsheet:
    def __init__(self, worksheet_name):
        self.worksheet_name = worksheet_name
        self.worksheet_calls = []

    def worksheet(self, name):
        self.worksheet_calls.append(name)
        return DummyWorksheet(name)


class DummyClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, sheet_id):
        assert sheet_id == "sheet-123"
        return self.spreadsheet


def test_google_sheet_repository_resolves_default_sheet(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-123")
    monkeypatch.setenv("GOOGLE_SHEET_NAME", "Leads")

    spreadsheet = DummySpreadsheet("Leads")
    client = DummyClient(spreadsheet)

    repo = GoogleSheetRepository(client=client)
    worksheet = repo.get_worksheet()

    assert worksheet.name == "Leads"
    assert spreadsheet.worksheet_calls == ["Leads"]


def test_sheet_service_delegates_to_repository():
    class FakeRepository:
        def __init__(self):
            self.calls = []

        def get_worksheet(self, name=None):
            self.calls.append(name)
            return {"name": name or "Leads"}

    repo = FakeRepository()
    service = SheetService(repository=repo)

    assert service.get_worksheet("Customers") == {"name": "Customers"}
    assert repo.calls == ["Customers"]


def test_sheet_columns_leads_has_essential_fields():
    from app.repositories.google_sheet import SHEET_COLUMNS
    assert "name" in SHEET_COLUMNS["leads"]
    assert "phone" in SHEET_COLUMNS["leads"]
    assert "email" in SHEET_COLUMNS["leads"]
    assert "requirements" in SHEET_COLUMNS["leads"]


def test_google_sheet_repository_user_and_lead_caching():
    from app.repositories.google_sheet import SHEET_COLUMNS, GoogleSheetRepository

    class MockWS:
        def __init__(self, name):
            self.name = name
            self.headers = list(SHEET_COLUMNS[name.lower()])
            self.rows = []
            self.col_count = len(self.headers)

        def row_values(self, row):
            return self.headers if row == 1 else []

        def get_all_values(self):
            return [self.headers] + self.rows

        def append_row(self, row, table_range="A1"):
            self.rows.append(row)
            row_idx = len(self.rows) + 1
            return {"updates": {"updatedRange": f"{self.name}!A{row_idx}:Z{row_idx}"}}

        def update(self, values, range_name=None):
            if range_name and values:
                # Update row based on range_name e.g. A2:Z2
                import re
                m = re.search(r"A(\d+)", range_name)
                if m:
                    idx = int(m.group(1)) - 2
                    if 0 <= idx < len(self.rows):
                        self.rows[idx] = list(values[0])

    class MockSS:
        def __init__(self):
            self.sheets = {k: MockWS(k) for k in SHEET_COLUMNS}
            self.title = "Mock Spreadsheet"

        def worksheets(self):
            return list(self.sheets.values())

        def worksheet(self, name):
            return self.sheets[name.lower()]

    mock_ss = MockSS()

    class MockClient:
        def open_by_key(self, key):
            return mock_ss

    repo = GoogleSheetRepository(client=MockClient())
    repo.check_ready()

    # Save user
    repo.save_user({
        "tenant_id": "client_001",
        "user_id": "u123",
        "name": "Alice",
        "email": "alice@example.com",
    })
    user = repo.get_user("client_001", "u123")
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"

    # Update same user
    repo.save_user({
        "tenant_id": "client_001",
        "user_id": "u123",
        "phone": "+1234567890",
    })
    user2 = repo.get_user("client_001", "u123")
    assert user2["name"] == "Alice"
    assert user2["phone"] == "+1234567890"
    # Should only be 1 row, not 2
    assert len(mock_ss.sheets["users"].rows) == 1

    # Save lead
    repo.save_lead({
        "tenant_id": "client_001",
        "lead_id": "lead_1",
        "conversation_id": "conv_1",
        "name": "Alice",
        "email": "alice@example.com",
        "status": "QUALIFIED",
    })
    lead = repo.get_lead("client_001", "lead_1")
    assert lead["conversation_id"] == "conv_1"
    assert lead["status"] == "QUALIFIED"

    # Warmup loads everything into cache
    repo.warm_up()
    assert ("client_001", "u123") in repo._user_cache
    assert ("client_001", "lead_1") in repo._lead_cache


def test_apps_script_repository(monkeypatch):
    from app.repositories.apps_script import AppsScriptRepository, CLEAN_HEADERS
    posted = []

    def mock_post(url, json=None, **kwargs):
        class DummyResp:
            status_code = 200
            text = '{"status": "ok"}'
        posted.append((url, json))
        return DummyResp()

    monkeypatch.setattr("httpx.post", mock_post)
    repo = AppsScriptRepository("https://script.google.com/macros/s/test/exec")

    # 1. Lead sync to mastersheet
    repo.save_lead({
        "tenant_id": "client_001",
        "user_id": "cust_123",
        "lead_id": "l1",
        "name": "Sarah Connor",
        "phone": "+1 (555) 019-2834",
        "email": "sarah@cyberdyne.io",
        "channel": "whatsapp",
        "service": "AI Chatbot",
        "requirements": "Need bot for store",
        "budget": "$5,000",
        "timeline": "1 month",
        "status": "QUALIFIED",
    })
    assert len(posted) == 1
    assert posted[0][1]["sheet"] == "mastersheet"
    assert posted[0][1]["headers"] == CLEAN_HEADERS["leads"]
    assert "Tenant" in posted[0][1]["headers"]
    assert "Customer ID" in posted[0][1]["headers"]
    assert posted[0][1]["keyField"] == "Customer ID"
    assert posted[0][1]["keyValue"] == "cust_123"
    assert "client_001" in posted[0][1]["row"]
    assert "cust_123" in posted[0][1]["row"]

    # 2. Lead with Booking Slot triggers sync to both mastersheet and bookings
    repo.save_lead({
        "tenant_id": "client_001",
        "user_id": "cust_123",
        "lead_id": "l1",
        "name": "Sarah Connor",
        "phone": "+1 (555) 019-2834",
        "booking_slot": "Tomorrow at 3 PM",
    })
    assert len(posted) == 3
    assert posted[1][1]["sheet"] == "mastersheet"
    assert posted[2][1]["sheet"] == "bookings"
    assert posted[2][1]["headers"] == CLEAN_HEADERS["bookings"]

    # 3. Message sync to chat history
    repo.save_message({
        "tenant_id": "client_001",
        "user_id": "cust_123",
        "channel": "whatsapp",
        "sender": "user",
        "message": "Hello bot",
    })
    assert len(posted) == 4
    assert posted[3][1]["sheet"] == "chat history"
    assert posted[3][1]["headers"] == CLEAN_HEADERS["messages"]
    assert posted[3][1]["row"] == ["", "client_001", "cust_123", "WHATSAPP", "User", "Hello bot"]

    # 4. Tenant sync to tenants
    repo.save_tenant({
        "tenant_id": "client_001",
        "company_name": "OrionSoft Technologies",
        "status": "active",
        "created_at": "2026-10-19",
    })
    assert len(posted) == 5
    assert posted[4][1]["sheet"] == "tenants"
    assert posted[4][1]["headers"] == CLEAN_HEADERS["tenants"]
    assert posted[4][1]["row"] == ["client_001", "OrionSoft Technologies", "active", "2026-10-19"]




