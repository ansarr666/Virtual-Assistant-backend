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
