"""In a group/channel, the bot must stay quiet unless directly addressed."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from maxapi.enums.chat_type import ChatType
from maxapi.types.message import MarkupUserMention

from bot.decorators import addressed_only
from bot.runtime import bot as runtime_bot

BOT_ID = 42
OTHER_ID = 99


def make_event(chat_type, *, mention_id=None, reply_sender_id=None):
    event = MagicMock()
    event.message.recipient.chat_type = chat_type
    event.message.link = None
    event.message.body.markup = []
    if mention_id is not None:
        event.message.body.markup = [
            MarkupUserMention(from_=0, length=0, user_id=mention_id)
        ]
    if reply_sender_id is not None:
        event.message.link = MagicMock()
        event.message.link.sender.user_id = reply_sender_id
    event.message.answer = AsyncMock()
    return event


@pytest.fixture(autouse=True)
def bot_id(monkeypatch):
    fake_me = MagicMock()
    fake_me.user_id = BOT_ID
    monkeypatch.setattr(runtime_bot, "me", fake_me)


@addressed_only
async def handler(event):
    return "прошёл"


@pytest.mark.asyncio
async def test_private_dialog_always_addressed():
    event = make_event(ChatType.DIALOG)
    assert await handler(event) == "прошёл"


@pytest.mark.asyncio
async def test_group_message_without_mention_or_reply_is_ignored():
    event = make_event(ChatType.CHAT)
    assert await handler(event) is None


@pytest.mark.asyncio
async def test_group_message_mentioning_bot_is_handled():
    event = make_event(ChatType.CHAT, mention_id=BOT_ID)
    assert await handler(event) == "прошёл"


@pytest.mark.asyncio
async def test_group_message_mentioning_someone_else_is_ignored():
    event = make_event(ChatType.CHAT, mention_id=OTHER_ID)
    assert await handler(event) is None


@pytest.mark.asyncio
async def test_reply_to_bot_message_is_handled():
    event = make_event(ChatType.CHANNEL, reply_sender_id=BOT_ID)
    assert await handler(event) == "прошёл"


@pytest.mark.asyncio
async def test_reply_to_someone_else_is_ignored():
    event = make_event(ChatType.CHANNEL, reply_sender_id=OTHER_ID)
    assert await handler(event) is None


@pytest.mark.asyncio
async def test_unknown_bot_id_fails_open(monkeypatch):
    monkeypatch.setattr(runtime_bot, "me", None)
    event = make_event(ChatType.CHAT)
    assert await handler(event) == "прошёл"
