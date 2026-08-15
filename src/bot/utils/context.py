"""Conversation context management."""
from collections import OrderedDict, deque
from typing import Any

import structlog
from sqlalchemy import select

from bot.database import Conversation, Setting, get_session

logger = structlog.get_logger()

# Key prefix for the per-user "context starts after this message" marker.
RESET_MARKER_PREFIX = "context_reset:"

# Cached contexts are a convenience, not the source of truth — anything evicted
# is rebuilt from the database on the next message. Bounding the cache keeps
# memory flat instead of growing with every user who ever wrote to the bot.
MAX_CACHED_CONTEXTS = 500


class ConversationContext:
    """Manages conversation context for users."""

    # In-memory storage for active contexts, least-recently-used first
    _contexts: OrderedDict[int, deque] = OrderedDict()

    def __init__(self, user_id: int, model_name: str, max_length: int = 4096):
        """Initialize conversation context."""
        self.user_id = user_id
        self.model_name = model_name
        self.max_length = max_length

    @property
    def _marker_key(self) -> str:
        return f"{RESET_MARKER_PREFIX}{self.user_id}"

    async def _reset_after_id(self, session) -> int:
        """Id of the last message hidden by the most recent reset."""
        raw = await session.scalar(
            select(Setting.value).where(Setting.key == self._marker_key)
        )
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            # A malformed marker must not wipe out the whole conversation.
            logger.warning("Bad context reset marker", user_id=self.user_id, value=raw)
            return 0

    async def get_context(self, message_limit: int = 10) -> list[dict[str, str]]:
        """Get conversation context for the user."""
        # Check in-memory cache first
        if self.user_id in self._contexts:
            self._contexts.move_to_end(self.user_id)
            messages = list(self._contexts[self.user_id])
            return messages[-message_limit:] if len(messages) > message_limit else messages

        # Load from database. Order by id, not created_at: SQLite fills
        # created_at from CURRENT_TIMESTAMP with one-second granularity, so
        # messages sent within the same second tie and come back in an
        # arbitrary order.
        async with get_session() as session:
            reset_after_id = await self._reset_after_id(session)
            result = await session.execute(
                select(Conversation)
                .where(
                    (Conversation.user_id == self.user_id)
                    & (Conversation.id > reset_after_id)
                )
                .order_by(Conversation.id.desc())
                .limit(message_limit)
            )
            db_messages = result.scalars().all()

        # Newest-first from the query, so reverse for chronological order
        messages = []
        for msg in reversed(db_messages):
            messages.append({
                "role": msg.message_role,
                "content": msg.message_content
            })

        self._cache(deque(messages, maxlen=message_limit * 2))

        return messages

    @classmethod
    def _evict_if_needed(cls) -> None:
        while len(cls._contexts) > MAX_CACHED_CONTEXTS:
            user_id, _ = cls._contexts.popitem(last=False)
            logger.debug("Evicted cached context", user_id=user_id)

    def _cache(self, buffer: deque) -> None:
        """Store a context buffer, evicting the least recently used ones."""
        self._contexts[self.user_id] = buffer
        self._contexts.move_to_end(self.user_id)
        self._evict_if_needed()

    async def add_message(self, role: str, content: str) ->  list[dict[str, str]]:
        """Add a message to the context and return the updated history."""
        message = {"role": role, "content": content}

        if self.user_id not in self._contexts:
            # Ensure existing history is loaded before appending a new message
            try:
                await self.get_context()
            except RuntimeError:
                logger.debug(
                    "Context database not initialized; using in-memory history only",
                    user_id=self.user_id,
                )

        if self.user_id not in self._contexts:
            self._cache(deque(maxlen=20))

        self._contexts.move_to_end(self.user_id)
        context_buffer = self._contexts[self.user_id]
        context_buffer.append(message)

        # Trim context if it's too long
        total_length = sum(len(m["content"]) for m in context_buffer)
        while total_length > self.max_length and len(context_buffer) > 2:
            context_buffer.popleft()
            total_length = sum(len(m["content"]) for m in context_buffer)

        return list(context_buffer)

    async def reset(self) -> None:
        """Start a fresh conversation for the user.

        Records the last stored message as the new starting point instead of
        deleting rows, so ``/history`` still works while the model no longer
        sees anything from before the reset.
        """
        async with get_session() as session:
            last_id = await session.scalar(
                select(Conversation.id)
                .where(Conversation.user_id == self.user_id)
                .order_by(Conversation.id.desc())
                .limit(1)
            )

            marker = await session.scalar(
                select(Setting).where(Setting.key == self._marker_key)
            )
            if marker:
                marker.value = str(last_id or 0)
            else:
                session.add(Setting(key=self._marker_key, value=str(last_id or 0)))
            await session.commit()

        self.forget(self.user_id)
        logger.info("Context reset", user_id=self.user_id, reset_after_id=last_id or 0)

    @classmethod
    def forget(cls, user_id: int) -> None:
        """Drop only the in-memory copy, keeping the stored history intact."""
        cls._contexts.pop(user_id, None)

    @classmethod
    def clear_all(cls) -> None:
        """Clear all contexts (for shutdown)."""
        cls._contexts.clear()
        logger.info("All contexts cleared")


def normalize_chat_messages(messages: list[Any]) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        raise TypeError("messages must be a list")

    norm: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role")
            content = m.get("content")
        elif isinstance(m, (list, tuple)) and len(m) == 2:
            role, content = m
        else:
            raise TypeError(f"Bad message item type: {type(m)} -> {m!r}")

        if not isinstance(role, str) or not isinstance(content, str):
            raise TypeError(f"role/content must be str, got: {type(role)}, {type(content)}")

        norm.append({"role": role, "content": content})
    return norm
