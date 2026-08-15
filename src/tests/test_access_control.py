"""Access control and rate limiting against a real database.

These paths decide who may talk to the bot, so they are exercised with actual
rows rather than mocks.
"""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.database import RateLimit, User
from bot.decorators import admin_only, authorized_only, rate_limited
from bot.utils.time import utc_now

ADMIN = 111
STRANGER = 999


def make_event(user_id: int):
    event = MagicMock()
    event.message.sender.user_id = user_id
    event.message.body.text = "привет"
    event.message.answer = AsyncMock()
    event.get_ids.return_value = (1, user_id)
    return event


def replies(event) -> list[str]:
    return [call.kwargs.get("text", "") for call in event.message.answer.await_args_list]


@pytest.fixture(autouse=True)
def admin_id(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ID", ADMIN)
    monkeypatch.setattr(settings, "TEST_MODE", False)


async def add_user(db, user_id: int, *, is_active: bool) -> None:
    async with db() as session:
        session.add(User(
            user_id=user_id, full_name=f"User {user_id}",
            is_active=is_active, is_admin=False,
        ))


@authorized_only
async def guarded(event):
    return "прошёл"


@admin_only
async def admin_guarded(event):
    return "прошёл"


@pytest.mark.asyncio
async def test_unknown_user_is_rejected(db):
    event = make_event(STRANGER)

    assert await guarded(event) is None
    assert "not authorized" in replies(event)[0]


@pytest.mark.asyncio
async def test_deactivated_user_is_rejected(db):
    await add_user(db, STRANGER, is_active=False)
    event = make_event(STRANGER)

    assert await guarded(event) is None


@pytest.mark.asyncio
async def test_active_user_passes(db):
    await add_user(db, STRANGER, is_active=True)
    event = make_event(STRANGER)

    assert await guarded(event) == "прошёл"
    event.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_mode_blocks_users_but_not_admin(db, monkeypatch):
    await add_user(db, STRANGER, is_active=True)
    monkeypatch.setattr(settings, "TEST_MODE", True)

    assert await guarded(make_event(STRANGER)) is None
    assert await guarded(make_event(ADMIN)) == "прошёл"


@pytest.mark.asyncio
async def test_admin_command_is_closed_to_others(db):
    await add_user(db, STRANGER, is_active=True)

    assert await admin_guarded(make_event(STRANGER)) is None
    assert await admin_guarded(make_event(ADMIN)) == "прошёл"


@rate_limited
async def limited(event):
    return "прошёл"


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_the_allowance(db, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_MESSAGES", 3)
    monkeypatch.setattr(settings, "RATE_LIMIT_WINDOW", 60)
    await add_user(db, STRANGER, is_active=True)

    results = [await limited(make_event(STRANGER)) for _ in range(4)]

    assert results[:3] == ["прошёл"] * 3
    assert results[3] is None


@pytest.mark.asyncio
async def test_allowance_returns_after_the_window(db, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_MESSAGES", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_WINDOW", 60)
    await add_user(db, STRANGER, is_active=True)

    assert await limited(make_event(STRANGER)) == "прошёл"
    assert await limited(make_event(STRANGER)) is None

    # Move the window into the past, as waiting would
    async with db() as session:
        row = await session.scalar(
            select(RateLimit).where(RateLimit.user_id == STRANGER)
        )
        row.window_start = utc_now() - timedelta(seconds=120)

    assert await limited(make_event(STRANGER)) == "прошёл"


@pytest.mark.asyncio
async def test_admin_is_not_rate_limited(db, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_MESSAGES", 1)

    results = [await limited(make_event(ADMIN)) for _ in range(5)]

    assert results == ["прошёл"] * 5
