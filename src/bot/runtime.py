"""Shared bot runtime objects.

MAX handlers are registered through module-level decorators and receive only the
update, so the ``Bot``, ``Dispatcher`` and ``OllamaClient`` instances live here
rather than being carried along on a per-update context object.
"""
from maxapi import Bot, Dispatcher

from bot.config import settings
from bot.utils.ollama import OllamaClient

bot = Bot(token=settings.MAX_BOT_TOKEN)
dp = Dispatcher()
ollama_client = OllamaClient(
    base_url=settings.OLLAMA_HOST,
    timeout=settings.OLLAMA_TIMEOUT,
)

__all__ = ["bot", "dp", "ollama_client"]
