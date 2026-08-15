"""Dispatch routing tests.

MAX hands an update to the FIRST matching handler, so the registration order in
``bot.handlers.__init__`` is the handler priority. These tests push real
``MessageCreated`` objects through the dispatcher and assert which handler wins.
"""
from unittest.mock import AsyncMock, patch

import pytest
from maxapi.types.updates.message_created import MessageCreated


def build_message_created(text: str | None = None, attachments: list | None = None):
    """Build a MessageCreated update as the MAX API would deliver it."""
    body: dict = {"mid": "mid-1", "seq": 1, "text": text}
    if attachments is not None:
        body["attachments"] = attachments

    return MessageCreated.model_validate({
        "update_type": "message_created",
        "timestamp": 1700000000,
        "message": {
            "sender": {
                "user_id": 777,
                "first_name": "Tester",
                "is_bot": False,
                "last_activity_time": 1700000000,
            },
            "recipient": {"chat_id": 42, "chat_type": "dialog"},
            "timestamp": 1700000000,
            "body": body,
        },
    })


@pytest.fixture
async def dispatcher():
    """Dispatcher with all handlers registered and a stubbed bot."""
    from maxapi.types.users import User

    from bot import handlers  # noqa: F401  (registers the handlers)
    from bot.runtime import bot, dp

    me = User(user_id=1, first_name="bot", username="ollama_bot",
              is_bot=True, last_activity_time=0)
    with patch.object(type(bot), "get_me", AsyncMock(return_value=me)):
        await dp.startup(bot)
    return dp


async def dispatch(dp, event) -> str | None:
    """Run one update through the dispatcher, returning the handler that ran.

    Every handler body is swapped for a recorder, so routing is exercised for
    real while no handler touches the database or the network.
    """
    event.bot = dp.bot  # the polling loop attaches the bot to every update
    called: list[str] = []
    originals = [h.func_event for h in dp.event_handlers]

    def recorder(name):
        async def run(event_object, **kwargs):
            called.append(name)
        return run

    for handler, original in zip(dp.event_handlers, originals, strict=True):
        handler.func_event = recorder(original.__name__)
    try:
        await dp.handle(event)
    finally:
        for handler, original in zip(dp.event_handlers, originals, strict=True):
            handler.func_event = original

    return called[0] if called else None


@pytest.mark.asyncio
async def test_command_beats_catch_all(dispatcher):
    """Commands must not be swallowed by the plain-text handler."""
    assert await dispatch(dispatcher, build_message_created("/start")) == "start"
    assert await dispatch(dispatcher, build_message_created("/help")) == "help_command"
    assert await dispatch(dispatcher, build_message_created("/models")) == "list_models"
    assert await dispatch(dispatcher, build_message_created("/clear")) == "clear_context"


@pytest.mark.asyncio
async def test_plain_text_goes_to_chat(dispatcher):
    """Ordinary text reaches the chat handler."""
    event = build_message_created("what is the capital of France?")
    assert await dispatch(dispatcher, event) == "handle_message"


@pytest.mark.asyncio
async def test_image_beats_catch_all(dispatcher):
    """A message with an attachment goes to the image handler, caption or not."""
    event = build_message_created(
        "describe this",
        attachments=[{
            "type": "image",
            "payload": {"photo_id": 1, "token": "t", "url": "https://example/i.jpg"},
        }],
    )
    assert await dispatch(dispatcher, event) == "handle_photo"


@pytest.mark.asyncio
async def test_command_args_are_parsed():
    """The Command filter yields the argument list handlers expect."""
    from maxapi.filters.command import Command

    event = build_message_created("/add_user 12345")
    event.bot = type("FakeBot", (), {"me": None})()

    assert await Command("add_user")(event) == {"args": ["12345"]}
