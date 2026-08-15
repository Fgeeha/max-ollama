"""Utilities package."""
from bot.utils.context import ConversationContext
from bot.utils.events import (
    answer,
    answer_html,
    event_chat_id,
    event_text,
    event_user,
    event_user_id,
)
from bot.utils.health import start_health_server, stop_health_server
from bot.utils.logging import setup_logging
from bot.utils.ollama import (
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
    OllamaModelNotFoundError,
)

__all__ = [
    "ConversationContext",
    "OllamaClient",
    "OllamaConnectionError",
    "OllamaError",
    "OllamaModelNotFoundError",
    "answer",
    "answer_html",
    "event_chat_id",
    "event_text",
    "event_user",
    "event_user_id",
    "setup_logging",
    "start_health_server",
    "stop_health_server",
]
