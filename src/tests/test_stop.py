"""/stop cancels a running generation."""
import asyncio
from unittest.mock import MagicMock

import pytest

from bot.handlers import chat

USER = 555


def make_event(user_id=USER):
    event = MagicMock()
    event.message.sender.user_id = user_id
    event.get_ids.return_value = (1, user_id)
    return event


@pytest.fixture(autouse=True)
def clean_registry():
    chat._generating.clear()
    yield
    chat._generating.clear()


@pytest.fixture(autouse=True)
def as_admin(monkeypatch):
    """Skip the authorization lookup; access control is tested separately."""
    from bot.config import settings

    monkeypatch.setattr(settings, "ADMIN_IDS", [USER])


@pytest.mark.asyncio
async def test_nothing_to_stop_is_reported(monkeypatch):
    replies = []
    monkeypatch.setattr(chat, "answer", lambda e, t, **k: _record(replies, t))

    await chat.stop_generation(make_event())

    assert "нечего останавливать" in replies[0]


@pytest.mark.asyncio
async def test_running_generation_is_cancelled(monkeypatch):
    replies = []
    monkeypatch.setattr(chat, "answer", lambda e, t, **k: _record(replies, t))

    started = asyncio.Event()

    async def long_generation():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(long_generation())
    await started.wait()
    chat._generating[USER] = task

    await chat.stop_generation(make_event())
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert "остановлена" in replies[0]


@pytest.mark.asyncio
async def test_one_user_cannot_stop_another(db, monkeypatch):
    from bot.database import User

    async with db() as session:
        session.add(User(user_id=999, full_name="Другой", is_active=True, is_admin=False))

    replies = []
    monkeypatch.setattr(chat, "answer", lambda e, t, **k: _record(replies, t))

    async def long_generation():
        await asyncio.sleep(30)

    task = asyncio.create_task(long_generation())
    chat._generating[USER] = task

    await chat.stop_generation(make_event(user_id=999))

    assert not task.cancelled()
    assert "нечего останавливать" in replies[0]

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _record(bucket, text):
    bucket.append(text)
