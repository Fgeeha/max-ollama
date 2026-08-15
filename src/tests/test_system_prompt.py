"""Per-user system prompt: show, set, reset."""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.database import User
from bot.handlers import chat

USER = 321


@pytest.fixture(autouse=True)
def as_admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ID", USER)


@pytest.fixture
def replies(monkeypatch):
    bucket = []

    async def fake(event, text, **kwargs):
        bucket.append(text)

    monkeypatch.setattr(chat, "answer", fake)
    monkeypatch.setattr(chat, "answer_html", fake)
    return bucket


def make_event():
    event = MagicMock()
    event.message.sender.user_id = USER
    event.get_ids.return_value = (1, USER)
    return event


async def seed_user(db) -> None:
    async with db() as session:
        session.add(User(user_id=USER, full_name="Тест", is_active=True, is_admin=True))


async def stored_prompt(db) -> str | None:
    async with db() as session:
        return await session.scalar(
            select(User.system_prompt).where(User.user_id == USER)
        )


@pytest.mark.asyncio
async def test_shows_the_default_when_unset(db, replies):
    await seed_user(db)

    await chat.system_prompt_command(make_event())

    assert "стандартный" in replies[0]


@pytest.mark.asyncio
async def test_setting_a_prompt_persists_it(db, replies):
    await seed_user(db)

    await chat.system_prompt_command(make_event(), args=["Ты", "пират.", "Отвечай", "как", "пират."])

    assert await stored_prompt(db) == "Ты пират. Отвечай как пират."
    assert "обновлён" in replies[0]


@pytest.mark.asyncio
async def test_reset_restores_the_default(db, replies):
    await seed_user(db)
    await chat.system_prompt_command(make_event(), args=["Ты", "пират."])

    await chat.system_prompt_command(make_event(), args=["reset"])

    assert await stored_prompt(db) is None


@pytest.mark.asyncio
async def test_overlong_prompt_is_refused(db, replies):
    await seed_user(db)

    await chat.system_prompt_command(
        make_event(), args=["x" * (chat.MAX_SYSTEM_PROMPT_CHARS + 1)]
    )

    assert await stored_prompt(db) is None
    assert "Слишком длинный" in replies[0]


@pytest.mark.asyncio
async def test_shows_the_custom_prompt_once_set(db, replies):
    await seed_user(db)
    await chat.system_prompt_command(make_event(), args=["Только", "факты."])
    replies.clear()

    await chat.system_prompt_command(make_event())

    assert "Только факты." in replies[0]
