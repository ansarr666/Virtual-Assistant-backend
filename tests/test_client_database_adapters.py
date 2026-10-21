import json
from unittest.mock import MagicMock
import pytest
import httpx

from app.repositories.sql import SqlLeadRepository
from app.repositories.supabase import SupabaseLeadRepository
from app.repositories.base import RepositoryError
from app.services.destination_service import DestinationService


class TestSqlLeadRepository:
    @pytest.fixture
    def repo(self):
        return SqlLeadRepository(connection_string=":memory:")

    def test_users_crud(self, repo):
        repo.save_user({
            "tenant_id": "client_abc",
            "user_id": "+1234567890",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "channel": "whatsapp",
            "custom_tag": "vip",
        })
        user = repo.get_user("client_abc", "+1234567890")
        assert user is not None
        assert user["name"] == "Alice Smith"
        assert user["email"] == "alice@example.com"
        assert user["custom_tag"] == "vip"

        # Update user
        repo.save_user({
            "tenant_id": "client_abc",
            "user_id": "+1234567890",
            "active_conversation_id": "conv_999",
        })
        updated = repo.get_user("client_abc", "+1234567890")
        assert updated["active_conversation_id"] == "conv_999"
        assert updated["name"] == "Alice Smith"  # Preserved

    def test_conversations_crud(self, repo):
        repo.save_conversation({
            "tenant_id": "client_abc",
            "conversation_id": "conv_123",
            "user_id": "+1234567890",
            "status": "active",
            "stage": "lead_capture",
        })
        conv = repo.get_conversation("client_abc", "conv_123")
        assert conv is not None
        assert conv["stage"] == "lead_capture"
        assert conv["status"] == "active"

        # Update conversation status
        repo.save_conversation({
            "tenant_id": "client_abc",
            "conversation_id": "conv_123",
            "status": "restarted",
        })
        updated = repo.get_conversation("client_abc", "conv_123")
        assert updated["status"] == "restarted"

    def test_messages_crud(self, repo):
        repo.save_message({
            "tenant_id": "client_abc",
            "conversation_id": "conv_123",
            "sender": "user",
            "message_text": "Hello there",
        })
        repo.save_messages([
            {
                "tenant_id": "client_abc",
                "conversation_id": "conv_123",
                "sender": "bot",
                "message_text": "How can I help you?",
            },
            {
                "tenant_id": "client_abc",
                "conversation_id": "conv_123",
                "sender": "user",
                "message_text": "I need custom software.",
            },
        ])
        # Verify directly in SQLite
        conn = repo._get_connection()
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id = 'conv_123'").fetchall()
        assert len(rows) == 3

    def test_state_crud(self, repo):
        repo.save_state({
            "tenant_id": "client_abc",
            "conversation_id": "conv_123",
            "current_step": "capture_email",
            "collected": {"name": "Bob"},
        })
        state = repo.get_state("client_abc", "conv_123")
        assert state is not None
        assert state["current_step"] == "capture_email"
        assert state["collected"]["name"] == "Bob"

    def test_leads_crud(self, repo):
        repo.save_lead({
            "tenant_id": "client_abc",
            "lead_id": "lead_555",
            "conversation_id": "conv_123",
            "user_id": "+1234567890",
            "service": "Mobile App Development",
            "requirements": "Need iOS and Android app",
            "timeline": "2-3 months",
            "budget": "$10k-$20k",
            "status": "qualified",
        })
        lead = repo.get_lead("client_abc", "lead_555")
        assert lead is not None
        assert lead["service"] == "Mobile App Development"
        assert lead["timeline"] == "2-3 months"
        assert lead["status"] == "qualified"

    def test_idempotency_crud(self, repo):
        response_data = {"reply": "Hi!", "quick_replies": []}
        repo.save_idempotent_response("client_abc", "conv_123", "msg_001", response_data)
        cached = repo.get_idempotent_response("client_abc", "conv_123", "msg_001")
        assert cached == response_data
        assert repo.get_idempotent_response("client_abc", "conv_123", "msg_other") is None

    def test_check_ready(self, repo):
        repo.check_ready()  # Should not raise


class TestSupabaseLeadRepository:
    @pytest.fixture
    def mock_client(self):
        return MagicMock(spec=httpx.Client)

    @pytest.fixture
    def repo(self, mock_client):
        return SupabaseLeadRepository(
            url="https://demo.supabase.co",
            key="test-anon-key",
            client=mock_client,
        )

    def test_save_user(self, repo, mock_client):
        mock_resp = MagicMock(status_code=201)
        mock_resp.raise_for_status.return_value = None
        mock_client.post.return_value = mock_resp

        repo.save_user({
            "tenant_id": "client_xyz",
            "user_id": "+9876543210",
            "name": "Jane Doe",
            "email": "jane@example.com",
        })
        assert mock_client.post.called
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://demo.supabase.co/rest/v1/users"
        assert kwargs["json"]["name"] == "Jane Doe"
        assert kwargs["headers"]["apikey"] == "test-anon-key"

    def test_get_user(self, repo, mock_client):
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [
            {"tenant_id": "client_xyz", "user_id": "+9876543210", "name": "Jane Doe"}
        ]
        mock_client.get.return_value = mock_resp

        user = repo.get_user("client_xyz", "+9876543210")
        assert user is not None
        assert user["name"] == "Jane Doe"

    def test_save_and_get_state(self, repo, mock_client):
        post_resp = MagicMock(status_code=201)
        post_resp.raise_for_status.return_value = None
        mock_client.post.return_value = post_resp

        repo.save_state({
            "tenant_id": "client_xyz",
            "conversation_id": "conv_99",
            "current_step": "welcome",
        })
        assert mock_client.post.called

        get_resp = MagicMock(status_code=200)
        get_resp.raise_for_status.return_value = None
        get_resp.json.return_value = [
            {"tenant_id": "client_xyz", "conversation_id": "conv_99", "state_json": {"current_step": "welcome"}}
        ]
        mock_client.get.return_value = get_resp

        state = repo.get_state("client_xyz", "conv_99")
        assert state is not None
        assert state["current_step"] == "welcome"


class TestDestinationServiceMultiTenantRouting:
    def test_custom_tenant_registration(self):
        dest_svc = DestinationService()
        sql_repo = SqlLeadRepository(connection_string=":memory:")

        dest_svc.register_tenant_repo("custom_client", sql_repo)

        dest_svc.save_user("custom_client", {
            "tenant_id": "custom_client",
            "user_id": "+1122334455",
            "name": "Custom Client User",
        })

        user = dest_svc.get_user("custom_client", "+1122334455")
        assert user is not None
        assert user["name"] == "Custom Client User"

        # Internal tenant still routes to default repo, not custom_client's repo
        dest_svc.unregister_tenant_repo("custom_client")
        assert dest_svc._tenant_repos.get("custom_client") is None
