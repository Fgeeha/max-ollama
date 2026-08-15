"""Helpers that normalize MAX update objects.

``MessageCreated`` and ``MessageCallback`` expose the sender and the reply
target in different places; handlers and decorators use these helpers so they
work with either event type.
"""
from typing import Any

from maxapi.enums.parse_mode import ParseMode
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.types.users import User

AnyEvent = MessageCreated | MessageCallback


def event_user(event: AnyEvent) -> User | None:
    """Return the user who produced the event."""
    if isinstance(event, MessageCallback):
        return event.callback.user
    return event.message.sender


def event_user_id(event: AnyEvent) -> int | None:
    """Return the user id of the event author."""
    user = event_user(event)
    return user.user_id if user else None


def event_chat_id(event: AnyEvent) -> int | None:
    """Return the chat id the event came from."""
    chat_id, _ = event.get_ids()
    return chat_id


def event_text(event: AnyEvent) -> str:
    """Return the message text (empty string when there is none)."""
    message = event.message
    body = message.body if message else None
    return (body.text if body and body.text else "") or ""


async def answer(event: AnyEvent, text: str, **kwargs: Any) -> Any:
    """Send a text reply to the event author, regardless of event type."""
    if isinstance(event, MessageCallback):
        return await event.send(text=text, **kwargs)
    return await event.message.answer(text=text, **kwargs)


async def answer_html(event: AnyEvent, text: str, **kwargs: Any) -> Any:
    """Send an HTML-formatted reply."""
    return await answer(event, text, format=ParseMode.HTML, **kwargs)


__all__ = [
    "AnyEvent",
    "answer",
    "answer_html",
    "event_chat_id",
    "event_text",
    "event_user",
    "event_user_id",
]
