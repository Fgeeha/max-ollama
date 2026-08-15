"""Conversation context: ordering and reset semantics.

These cover two defects found in review:
  * history was ordered by ``created_at``, which SQLite fills with a
    second-granularity timestamp — messages sent within the same second tied and
    came back reverse-chronological, so the model read the dialogue backwards;
  * ``/clear`` dropped only the in-memory copy, so the next message reloaded the
    very same history from the database.
"""
import pytest

from bot.utils.context import ConversationContext

from .conftest import add_messages


@pytest.mark.asyncio
async def test_history_is_chronological_within_one_second(db):
    """Messages written in the same second keep their insertion order."""
    await add_messages(
        db, 1,
        ("user", "msg0"), ("assistant", "msg1"),
        ("user", "msg2"), ("assistant", "msg3"),
    )

    messages = await ConversationContext(1, "m").get_context()

    assert [m["content"] for m in messages] == ["msg0", "msg1", "msg2", "msg3"]


@pytest.mark.asyncio
async def test_history_limit_keeps_the_newest_messages(db):
    """When history is truncated, the recent turns survive, not the oldest."""
    await add_messages(db, 1, *[("user", f"msg{i}") for i in range(15)])

    messages = await ConversationContext(1, "m").get_context(message_limit=5)

    assert [m["content"] for m in messages] == ["msg10", "msg11", "msg12", "msg13", "msg14"]


@pytest.mark.asyncio
async def test_reset_survives_reload_from_database(db):
    """After a reset the next request starts from an empty context."""
    await add_messages(db, 1, ("user", "вопрос"), ("assistant", "ответ"))

    await ConversationContext(1, "m").reset()

    messages = await ConversationContext(1, "m").get_context()
    assert messages == []


@pytest.mark.asyncio
async def test_reset_keeps_rows_for_history_command(db):
    """Reset hides history from the model but leaves it readable via /history."""
    await add_messages(db, 1, ("user", "вопрос"), ("assistant", "ответ"))

    await ConversationContext(1, "m").reset()

    from sqlalchemy import func, select

    from bot.database import Conversation
    async with db() as session:
        total = await session.scalar(
            select(func.count(Conversation.id)).where(Conversation.user_id == 1)
        )
    assert total == 2


@pytest.mark.asyncio
async def test_reset_is_per_user(db):
    """One user's reset does not touch another user's context."""
    await add_messages(db, 1, ("user", "мой вопрос"))
    await add_messages(db, 2, ("user", "чужой вопрос"))

    await ConversationContext(1, "m").reset()

    assert await ConversationContext(1, "m").get_context() == []
    other = await ConversationContext(2, "m").get_context()
    assert [m["content"] for m in other] == ["чужой вопрос"]


@pytest.mark.asyncio
async def test_new_messages_after_reset_are_visible(db):
    """Messages written after a reset form the new context."""
    await add_messages(db, 1, ("user", "старое"))
    await ConversationContext(1, "m").reset()

    await add_messages(db, 1, ("user", "новое"))
    ConversationContext.clear_all()  # force a reload from the database

    messages = await ConversationContext(1, "m").get_context()
    assert [m["content"] for m in messages] == ["новое"]


@pytest.mark.asyncio
async def test_admin_clearing_history_drops_cached_context(db):
    """Deleted history must not keep reaching the model from the in-memory cache."""
    from bot.database import Conversation

    await add_messages(db, 1, ("user", "секрет"))
    # Warm the cache the way a normal chat turn would.
    assert await ConversationContext(1, "m").get_context()

    async with db() as session:
        await session.execute(Conversation.__table__.delete())
        await session.commit()
    ConversationContext.forget(1)

    assert await ConversationContext(1, "m").get_context() == []


@pytest.mark.asyncio
async def test_cache_is_bounded_and_evicts_least_recently_used(db, monkeypatch):
    """Memory stays flat no matter how many users ever wrote to the bot."""
    from bot.utils import context as context_module

    monkeypatch.setattr(context_module, "MAX_CACHED_CONTEXTS", 3)

    for user_id in range(1, 5):
        await add_messages(db, user_id, ("user", f"привет от {user_id}"))
        await ConversationContext(user_id, "m").get_context()

    cached = list(ConversationContext._contexts)
    assert len(cached) == 3
    assert 1 not in cached, "самый давний пользователь должен быть вытеснен"
    assert cached[-1] == 4


@pytest.mark.asyncio
async def test_evicted_context_is_rebuilt_from_database(db, monkeypatch):
    """Eviction loses nothing: the next message reloads the history."""
    from bot.utils import context as context_module

    monkeypatch.setattr(context_module, "MAX_CACHED_CONTEXTS", 1)

    await add_messages(db, 1, ("user", "мой вопрос"))
    await ConversationContext(1, "m").get_context()

    await add_messages(db, 2, ("user", "чужой"))
    await ConversationContext(2, "m").get_context()   # вытесняет пользователя 1

    assert 1 not in ConversationContext._contexts
    restored = await ConversationContext(1, "m").get_context()
    assert [m["content"] for m in restored] == ["мой вопрос"]
