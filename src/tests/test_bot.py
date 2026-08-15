"""Tests for the MAX Ollama Bot."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.config import Settings
from bot.database.models import Conversation, User
from bot.utils.ollama import OllamaClient, OllamaModelNotFoundError


@pytest.fixture
def mock_settings():
    """Create mock settings for testing."""
    return Settings(
        MAX_BOT_TOKEN="test_token",
        ADMIN_ID=123456789,
        OLLAMA_HOST="http://localhost:11434",
        DATABASE_URL="sqlite:///test.db",
        TEST_MODE=False,
    )


@pytest.fixture
async def ollama_client():
    """Create Ollama client for testing."""
    client = OllamaClient("http://localhost:11434")
    yield client
    await client.close()


class TestOllamaClient:
    """Test Ollama client functionality."""

    @pytest.mark.asyncio
    async def test_list_models(self, ollama_client):
        """Test listing models."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "models": [
                {"name": "llama2", "size": 3826793472},
                {"name": "codellama", "size": 4212735980}
            ]
        }

        with patch.object(ollama_client.client, 'get', AsyncMock(return_value=mock_response)):
            models = await ollama_client.list_models()

            assert len(models) == 2
            assert models[0]["name"] == "llama2"
            assert models[1]["name"] == "codellama"

    @pytest.mark.asyncio
    async def test_model_exists(self, ollama_client):
        """Test checking if model exists."""
        ollama_client._available_models = [
            {"name": "llama2:latest"},
            {"name": "codellama:7b"}
        ]
        ollama_client._last_model_check = datetime.now().timestamp()

        assert await ollama_client.model_exists("llama2:latest") is True
        assert await ollama_client.model_exists("llama2") is True
        assert await ollama_client.model_exists("nonexistent") is False

    @pytest.mark.asyncio
    async def test_chat_error_on_missing_model(self, ollama_client):
        """Chatting with a model that is not installed fails fast."""
        ollama_client._available_models = []
        ollama_client._last_model_check = datetime.now().timestamp()

        with pytest.raises(OllamaModelNotFoundError):
            async for _ in ollama_client.chat_stream(
                "nonexistent", [{"role": "user", "content": "hi"}]
            ):
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model,info,expected",
        [
            # Modern Ollama: "capabilities" is authoritative, ignore everything else.
            ("llava:13b", {"capabilities": ["completion", "vision"]}, True),
            ("qwen2.5:14b", {"capabilities": ["completion", "tools"]}, False),
            # A system prompt mentioning images must not fool the capability check.
            ("llama2:7b", {"capabilities": ["completion"], "system": "describe the image"}, False),
            # Legacy Ollama without "capabilities": fall back to families/name.
            ("llava:13b", {"details": {"families": ["llama", "clip"]}}, True),
            ("llama3.2-vision", {"details": {"families": ["mllama"]}}, True),
            ("llama2:7b", {"details": {"families": ["llama"]}}, False),
        ],
    )
    async def test_supports_images(self, ollama_client, model, info, expected):
        """Vision detection prefers /api/show capabilities, falls back to name sniffing."""
        with patch.object(ollama_client, 'show_model_info', AsyncMock(return_value=info)):
            assert await ollama_client.supports_images(model) is expected

    @pytest.mark.asyncio
    async def test_supports_images_falls_back_to_false(self, ollama_client):
        """An unreachable /api/show must not block text chat."""
        from bot.utils.ollama import OllamaError

        with patch.object(
            ollama_client, 'show_model_info', AsyncMock(side_effect=OllamaError("boom"))
        ):
            assert await ollama_client.supports_images("llama2") is False


class TestDatabaseModels:
    """Test database models."""

    def test_user_model(self):
        """Test User model creation."""
        user = User(
            user_id=123456,
            username="testuser",
            full_name="Test User",
            is_active=True,
            is_admin=False
        )

        assert user.user_id == 123456
        assert user.username == "testuser"
        assert user.full_name == "Test User"
        assert user.is_active is True
        assert user.is_admin is False

    def test_conversation_model(self):
        """Test Conversation model creation."""
        conv = Conversation(
            user_id=123456,
            model_name="llama2",
            message_role="user",
            message_content="Hello, bot!"
        )

        assert conv.user_id == 123456
        assert conv.model_name == "llama2"
        assert conv.message_role == "user"
        assert conv.message_content == "Hello, bot!"


def make_message_event(user_id: int, text: str = "/cmd"):
    """Build a MessageCreated-like mock understood by the event helpers."""
    event = MagicMock()
    event.message.sender.user_id = user_id
    event.message.body.text = text
    event.message.answer = AsyncMock()
    event.get_ids.return_value = (555, user_id)
    return event


class TestEventHelpers:
    """Test the MAX event normalization helpers."""

    def test_event_user_id_and_text(self):
        from bot.utils.events import event_text, event_user_id

        event = make_message_event(42, "hello")
        assert event_user_id(event) == 42
        assert event_text(event) == "hello"

    def test_event_text_without_body(self):
        from bot.utils.events import event_text

        event = MagicMock()
        event.message.body = None
        assert event_text(event) == ""

    @pytest.mark.asyncio
    async def test_answer_uses_message_answer(self):
        from bot.utils.events import answer

        event = make_message_event(42)
        await answer(event, "hi")
        event.message.answer.assert_awaited_once_with(text="hi")


class TestDecorators:
    """Test decorators."""

    @pytest.mark.asyncio
    async def test_admin_only_decorator(self):
        """Test admin_only decorator."""
        from bot.decorators import admin_only

        @admin_only
        async def admin_command(event):
            return "admin_success"

        event = make_message_event(999)  # Non-admin

        with patch('bot.decorators.settings.ADMIN_ID', 123456789):
            result = await admin_command(event)

            assert result is None
            event.message.answer.assert_awaited_once()

            # Test with admin user
            event.message.sender.user_id = 123456789
            event.get_ids.return_value = (555, 123456789)
            event.message.answer.reset_mock()

            result = await admin_command(event)
            assert result == "admin_success"
            event.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_only_decorator(self):
        """Test authorized_only decorator with test mode."""
        from bot.decorators import authorized_only

        @authorized_only
        async def user_command(event):
            return "user_success"

        event = make_message_event(999)

        # Test with TEST_MODE enabled
        with patch('bot.decorators.settings.TEST_MODE', True), \
             patch('bot.decorators.settings.ADMIN_ID', 123456789):

            result = await user_command(event)
            assert result is None
            event.message.answer.assert_awaited_once_with(
                text="🔒 Bot is in test mode. Only administrators can interact."
            )

    @pytest.mark.asyncio
    async def test_decorator_forwards_command_args(self):
        """The dispatcher passes ``args`` as a kwarg; decorators must forward it."""
        import inspect

        from bot.decorators import admin_only

        @admin_only
        async def admin_command(event, args=None):
            return args

        # The dispatcher inspects the signature to decide which kwargs to pass.
        assert "args" in inspect.signature(admin_command).parameters

        event = make_message_event(123456789)
        with patch('bot.decorators.settings.ADMIN_ID', 123456789):
            assert await admin_command(event, args=["1", "2"]) == ["1", "2"]


class TestConversationContext:
    """Test conversation context management."""

    @pytest.mark.asyncio
    async def test_add_and_get_context(self):
        """Test adding and retrieving context."""
        from bot.utils.context import ConversationContext

        ConversationContext.clear_all()
        context = ConversationContext(user_id=123, model_name="llama2")

        # Add messages
        await context.add_message("user", "Hello")
        await context.add_message("assistant", "Hi there!")

        # Get context
        messages = await context.get_context()

        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_context_trimming(self):
        """Context is trimmed once it exceeds the token budget."""
        from bot.utils.context import ConversationContext
        from bot.utils.tokens import estimate_messages_tokens

        ConversationContext.clear_all()
        context = ConversationContext(user_id=456, model_name="llama2", max_tokens=30)

        await context.add_message("user", "A" * 50)
        await context.add_message("assistant", "B" * 50)
        await context.add_message("user", "C" * 50)

        messages = await context.get_context()

        # The oldest message is dropped first; the last exchange always stays,
        # even when it alone exceeds the budget.
        assert [m["content"][0] for m in messages] == ["B", "C"]
        assert estimate_messages_tokens(messages) < estimate_messages_tokens([
            {"role": "user", "content": "A" * 50},
            {"role": "assistant", "content": "B" * 50},
            {"role": "user", "content": "C" * 50},
        ])


@pytest.mark.asyncio
async def test_health_check_server():
    """Test health check server."""
    from aiohttp import ClientSession

    from bot.utils.health import start_health_server, stop_health_server

    mock_ollama = AsyncMock()
    mock_ollama.health_check.return_value = True

    # Start server
    runner = await start_health_server(mock_ollama, port=8888)

    try:
        # Test health endpoint
        async with ClientSession() as session:
            async with session.get("http://localhost:8888/health") as response:
                assert response.status == 200
                data = await response.json()
                assert data["status"] == "healthy"
                assert data["bot"] == "online"
    finally:
        await stop_health_server(runner)
