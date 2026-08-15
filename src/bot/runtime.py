"""Shared bot runtime objects.

MAX handlers are registered through module-level decorators and receive only the
update, so the ``Bot``, ``Dispatcher`` and ``OllamaClient`` instances live here
rather than being carried along on a per-update context object.
"""
from maxapi import Bot, Dispatcher

from bot.config import settings
from bot.utils.ollama import OllamaClient

bot = Bot(token=settings.MAX_BOT_TOKEN)

# use_create_task: without it the dispatcher awaits each update in turn, so one
# generation blocks every other chat and the polling loop itself.
dp = Dispatcher(use_create_task=True)

ollama_client = OllamaClient(
    base_url=settings.OLLAMA_HOST,
    timeout=settings.OLLAMA_TIMEOUT,
    stream_read_timeout=settings.OLLAMA_STREAM_READ_TIMEOUT,
)

__all__ = ["bot", "dp", "ollama_client"]
