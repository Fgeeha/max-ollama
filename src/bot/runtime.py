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

# Only pass options the operator actually set; anything else is left to the
# model's own defaults.
_options = {
    name: value
    for name, value in (
        ("temperature", settings.OLLAMA_TEMPERATURE),
        ("num_ctx", settings.OLLAMA_NUM_CTX),
    )
    if value is not None
}

ollama_client = OllamaClient(
    base_url=settings.OLLAMA_HOST,
    timeout=settings.OLLAMA_TIMEOUT,
    stream_read_timeout=settings.OLLAMA_STREAM_READ_TIMEOUT,
    generation_timeout=settings.OLLAMA_GENERATION_TIMEOUT,
    keep_alive=settings.OLLAMA_KEEP_ALIVE,
    options=_options,
    api_key=settings.OLLAMA_API_KEY,
    api_style=settings.OLLAMA_API_STYLE,
)

__all__ = ["bot", "dp", "ollama_client"]
