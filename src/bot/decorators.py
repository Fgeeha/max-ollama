"""Decorators for bot handlers."""
import functools
from collections.abc import Callable
from datetime import timedelta

import structlog
from sqlalchemy import select

from bot.config import settings
from bot.database import RateLimit, User, get_session
from bot.utils.events import (
    AnyEvent,
    answer,
    answer_html,
    event_text,
    event_user_id,
    is_addressed_to_bot,
    is_group_chat,
)
from bot.utils.time import utc_now

logger = structlog.get_logger()


def addressed_only(func: Callable) -> Callable:
    """Silently ignore group/channel messages that don't target the bot.

    Without an @-mention or a reply to one of its own messages, the bot
    should not join in on chatter in a chat/channel it has been added to.
    """
    @functools.wraps(func)
    async def wrapper(event: AnyEvent, *args, **kwargs):
        from bot.runtime import bot

        bot_user_id = bot.me.user_id if bot.me else None
        if is_group_chat(event) and not is_addressed_to_bot(event, bot_user_id):
            return None

        return await func(event, *args, **kwargs)

    return wrapper


def admin_only(func: Callable) -> Callable:
    """Decorator to restrict access to admin only."""
    @functools.wraps(func)
    async def wrapper(event: AnyEvent, *args, **kwargs):
        user_id = event_user_id(event)

        if user_id not in settings.ADMIN_IDS:
            await answer(
                event,
                "❌ This command is restricted to administrators only."
            )
            logger.warning(
                "Unauthorized admin command attempt",
                user_id=user_id,
                command=event_text(event)
            )
            return

        return await func(event, *args, **kwargs)

    return wrapper


def authorized_only(func: Callable) -> Callable:
    """Decorator to restrict access to authorized users only."""
    @functools.wraps(func)
    async def wrapper(event: AnyEvent, *args, **kwargs):
        user_id = event_user_id(event)

        # Admin always has access
        if user_id in settings.ADMIN_IDS:
            return await func(event, *args, **kwargs)

        # Check test mode
        if settings.TEST_MODE:
            await answer(
                event,
                "🔒 Bot is in test mode. Only administrators can interact."
            )
            return

        # Check user authorization
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.user_id == user_id)
            )
            user = result.scalar_one_or_none()

            if not user or not user.is_active:
                await answer_html(
                    event,
                    "❌ You are not authorized to use this bot.\n\n"
                    f"Please contact the administrator with your User ID: <code>{user_id}</code>"
                )
                logger.warning(
                    "Unauthorized access attempt",
                    user_id=user_id,
                    command=event_text(event) or "callback"
                )
                return

        return await func(event, *args, **kwargs)

    return wrapper


def rate_limited(func: Callable) -> Callable:
    """Decorator to apply rate limiting to handlers."""
    @functools.wraps(func)
    async def wrapper(event: AnyEvent, *args, **kwargs):
        user_id = event_user_id(event)

        # Admin bypasses rate limiting
        if user_id in settings.ADMIN_IDS:
            return await func(event, *args, **kwargs)

        async with get_session() as session:
            # Get or create rate limit record
            result = await session.execute(
                select(RateLimit).where(RateLimit.user_id == user_id)
            )
            rate_limit = result.scalar_one_or_none()

            current_time = utc_now()

            if not rate_limit:
                # Create new rate limit record
                rate_limit = RateLimit(
                    user_id=user_id,
                    message_count=1,
                    window_start=current_time,
                    last_reset=current_time
                )
                session.add(rate_limit)
                await session.commit()
                return await func(event, *args, **kwargs)

            # Check if we need to reset the window
            window_duration = timedelta(seconds=settings.RATE_LIMIT_WINDOW)
            if current_time - rate_limit.window_start > window_duration:
                # Reset the window
                rate_limit.message_count = 1
                rate_limit.window_start = current_time
                rate_limit.last_reset = current_time
                await session.commit()
                return await func(event, *args, **kwargs)

            # Check rate limit
            if rate_limit.message_count >= settings.RATE_LIMIT_MESSAGES:
                # Calculate time until reset
                reset_time = rate_limit.window_start + window_duration
                seconds_until_reset = (reset_time - current_time).total_seconds()

                await answer(
                    event,
                    f"⏱️ Rate limit exceeded!\n\n"
                    f"You've sent {rate_limit.message_count} messages in the last "
                    f"{settings.RATE_LIMIT_WINDOW} seconds.\n"
                    f"Please wait {int(seconds_until_reset)} seconds before sending more messages."
                )

                logger.warning(
                    "Rate limit exceeded",
                    user_id=user_id,
                    message_count=rate_limit.message_count,
                    seconds_until_reset=int(seconds_until_reset)
                )
                return

            # Increment message count
            rate_limit.message_count += 1
            await session.commit()

        return await func(event, *args, **kwargs)

    return wrapper
