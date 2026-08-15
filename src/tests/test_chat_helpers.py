"""Small chat-handler helpers: image history and the typing indicator."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.handlers.chat import IMAGE_PLACEHOLDER_PREFIX, _keep_typing, _mark_past_images


def test_plain_message_is_untouched():
    message = {"role": "user", "content": "как дела?"}
    assert _mark_past_images(message) == message


def test_assistant_message_is_untouched():
    message = {"role": "assistant", "content": f"{IMAGE_PLACEHOLDER_PREFIX} что-то"}
    assert _mark_past_images(message) == message


def test_past_image_is_marked_as_unavailable():
    """The model must not think it still has the picture."""
    message = {
        "role": "user",
        "content": f"{IMAGE_PLACEHOLDER_PREFIX} что на фото?",
    }

    marked = _mark_past_images(message)

    assert "недоступно" in marked["content"]
    assert "что на фото?" in marked["content"]
    assert not marked["content"].startswith(IMAGE_PLACEHOLDER_PREFIX)


@pytest.mark.asyncio
async def test_typing_is_refreshed_until_cancelled(monkeypatch):
    from bot.handlers import chat

    monkeypatch.setattr(chat, "TYPING_REFRESH_INTERVAL", 0.01)
    send_action = AsyncMock()
    monkeypatch.setattr(chat.bot, "send_action", send_action)

    task = asyncio.create_task(_keep_typing(1, chat.SenderAction.TYPING_ON))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert send_action.await_count >= 2, "индикатор должен обновляться повторно"


@pytest.mark.asyncio
async def test_typing_without_chat_id_does_nothing(monkeypatch):
    from bot.handlers import chat

    send_action = AsyncMock()
    monkeypatch.setattr(chat.bot, "send_action", send_action)

    await _keep_typing(None, chat.SenderAction.TYPING_ON)

    send_action.assert_not_awaited()
