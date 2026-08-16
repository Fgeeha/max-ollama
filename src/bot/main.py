"""Main entry point for the MAX Ollama Bot."""
import asyncio
import signal
import sys

import structlog

from bot import handlers  # noqa: F401  (import registers all handlers on `dp`)
from bot.config import settings
from bot.database.connection import close_database, init_database
from bot.runtime import bot, dp, ollama_client
from bot.utils.health import start_health_server, stop_health_server
from bot.utils.logging import setup_logging
from bot.utils.runtime_settings import load_into_settings

logger = structlog.get_logger()


class BotApplication:
    """Main bot application class."""

    def __init__(self) -> None:
        self.health_server = None

    async def initialize(self) -> None:
        """Initialize all bot components."""
        logger.info("Initializing bot application...")

        await init_database()
        logger.info("Database initialized")

        await load_into_settings()

        await ollama_client.verify_connection()
        logger.info("Ollama client initialized", host=settings.OLLAMA_HOST)

        if settings.HEALTH_CHECK_ENABLED:
            self.health_server = await start_health_server(
                ollama_client,
                settings.HEALTH_CHECK_PORT,
            )
            logger.info("Health check server started", port=settings.HEALTH_CHECK_PORT)

    async def start(self) -> None:
        """Start receiving updates. Returns when the transport stops."""
        logger.info("Starting bot...", mode=settings.BOT_MODE, test_mode=settings.TEST_MODE)
        if settings.TEST_MODE:
            logger.warning("Bot is running in TEST MODE - only admin can interact")

        if settings.BOT_MODE == "webhook":
            assert settings.WEBHOOK_URL is not None  # enforced by Settings validation
            await bot.subscribe_webhook(url=settings.WEBHOOK_URL, secret=settings.WEBHOOK_SECRET)
            await dp.handle_webhook(
                bot,
                host=settings.WEBHOOK_HOST,
                port=settings.WEBHOOK_PORT,
                path=settings.WEBHOOK_PATH,
                secret=settings.WEBHOOK_SECRET,
            )
        else:
            await dp.start_polling(bot)

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("Stopping bot...")

        if settings.BOT_MODE == "webhook":
            assert settings.WEBHOOK_URL is not None  # enforced by Settings validation
            await bot.unsubscribe_webhook(url=settings.WEBHOOK_URL)
        else:
            await dp.stop_polling()
        await bot.close_session()

        if self.health_server:
            await stop_health_server(self.health_server)

        await ollama_client.close()
        await close_database()

        logger.info("Bot stopped")

    async def run(self) -> None:
        """Run the bot until interrupted."""
        await self.initialize()
        try:
            await self.start()
        finally:
            await self.stop()


async def main() -> None:
    """Main function."""
    setup_logging(settings.LOG_LEVEL)

    logger.info(
        "Starting MAX Ollama Bot",
        version="1.0.0",
        admin_ids=settings.ADMIN_IDS,
        test_mode=settings.TEST_MODE,
    )

    app = BotApplication()
    task = asyncio.ensure_future(app.run())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)

    try:
        await task
    except asyncio.CancelledError:
        logger.info("Shutdown signal received")
    except Exception as e:
        logger.exception("Fatal error in main", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
