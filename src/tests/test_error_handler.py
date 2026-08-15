"""A crashing handler must still produce a reply to the user."""
from unittest.mock import AsyncMock, patch

import pytest
from maxapi.types.users import User

from .test_routing import build_message_created


@pytest.mark.asyncio
async def test_user_is_told_when_a_handler_crashes():
    from bot import handlers  # noqa: F401
    from bot.handlers import errors
    from bot.runtime import bot, dp

    me = User(user_id=1, first_name="b", username="b", is_bot=True, last_activity_time=0)
    with patch.object(type(bot), "get_me", AsyncMock(return_value=me)):
        await dp.startup(bot)

    replies = []

    async def fake_answer(event, text, **kwargs):
        replies.append(text)

    async def boom(event, **kwargs):
        raise RuntimeError("модель взорвалась")

    target = next(h for h in dp.event_handlers if h.func_event.__name__ == "handle_message")
    original = target.func_event
    target.func_event = boom
    try:
        with patch.object(errors, "answer", fake_answer):
            event = build_message_created("привет")
            event.bot = bot
            await dp.handle(event)
    finally:
        target.func_event = original

    assert len(replies) == 1
    assert "не так" in replies[0]
