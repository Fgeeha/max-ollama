"""Catch-all error handler.

Without it an exception raised outside a handler's own ``try`` only reaches the
log, and the user is left waiting for a reply that will never arrive.
"""
import structlog
from maxapi.types.error_event import ErrorEvent

from bot.runtime import dp
from bot.utils.events import answer, event_user_id

logger = structlog.get_logger()


@dp.errors()
async def on_error(event: ErrorEvent) -> None:
    """Log the failure with a traceback and tell the user something broke."""
    update = event.update

    logger.error(
        "Unhandled error in handler",
        process_info=event.process_info,
        exc_info=event.exception,
    )

    try:
        await answer(
            update,
            "❌ Что-то пошло не так при обработке сообщения.\n"
            "Попробуйте ещё раз, а если повторится — сообщите администратору."
        )
    except Exception:
        # The failure may itself be "cannot send a message" — never let the
        # error handler raise a second exception on top of the first.
        logger.warning(
            "Could not notify the user about an error",
            user_id=event_user_id(update) if update is not None else None,
        )
