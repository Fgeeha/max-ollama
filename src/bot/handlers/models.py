"""User command handlers for model management."""
from html import escape

import structlog
from maxapi.filters.callback_payload import CallbackPayload
from maxapi.filters.command import Command
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from bot.config import settings
from bot.database import User, get_session
from bot.decorators import authorized_only
from bot.runtime import dp, ollama_client
from bot.utils.events import answer, answer_html, event_user_id

logger = structlog.get_logger()


class SelectModelPayload(CallbackPayload, prefix="select_model"):
    """Payload carrying the model chosen from the inline keyboard."""

    name: str


@dp.message_created(Command("models"))
@authorized_only
async def list_models(event: MessageCreated) -> None:
    """List available Ollama models."""
    try:
        models = await ollama_client.list_models()

        if not models:
            await answer(
                event,
                "❌ No models available. Please ensure Ollama has models installed."
            )
            return

        # Get current user's selected model
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.user_id == event_user_id(event))
            )
            user = result.scalar_one_or_none()
            current_model = user.selected_model if user else None

        # Create inline keyboard for model selection
        keyboard = InlineKeyboardBuilder()
        for model in models:
            model_name = model["name"]
            # Format size
            size_gb = model.get("size", 0) / (1024**3)
            size_str = f"{size_gb:.1f}GB"

            # Add checkmark for current model
            is_current = model_name == current_model
            display_name = f"{'✅ ' if is_current else ''}{model_name} ({size_str})"

            keyboard.row(
                CallbackButton(
                    text=display_name,
                    payload=SelectModelPayload(name=model_name).pack(),
                )
            )

        message = (
            "🤖 <b>Available Models:</b>\n\n"
            "Select a model to use for conversations:"
        )

        if current_model:
            message += f"\n\n<i>Current model: {current_model}</i>"

        await answer_html(event, message, attachments=[keyboard.as_markup()])

    except Exception:
        logger.exception("Failed to list models")
        await answer(event, "❌ Failed to retrieve model list. Please try again later.")


@dp.message_created(Command("switch_model"))
@authorized_only
async def switch_model(event: MessageCreated, args: list[str] | None = None) -> None:
    """Switch to a different model."""
    if not args:
        await answer(
            event,
            "Usage: /switch_model <model_name>\n"
            "Example: /switch_model llama2\n\n"
            "Use /models to see available models."
        )
        return

    model_name = args[0]

    # Validate model exists
    try:
        if not await ollama_client.model_exists(model_name):
            available = await ollama_client.get_model_names()
            await answer(
                event,
                f"❌ Model '{model_name}' not found.\n\n"
                f"Available models: {', '.join(available)}"
            )
            return
    except Exception:
        logger.exception("Failed to validate model")
        await answer(event, "❌ Failed to validate model. Please try again later.")
        return

    # Update user's selected model
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == event_user_id(event))
        )
        user = result.scalar_one_or_none()

        if not user:
            await answer(event, "❌ User not found. Please contact the administrator.")
            return

        user.selected_model = model_name

    await answer_html(event, f"✅ Switched to model: <b>{model_name}</b>")

    logger.info("User switched model", user_id=event_user_id(event), model=model_name)


@dp.message_callback(SelectModelPayload.filter())
async def handle_model_selection(
    event: MessageCallback, payload: SelectModelPayload
) -> None:
    """Handle model selection from the inline keyboard."""
    user_id = event.callback.user.user_id
    model_name = payload.name

    # Update user's selected model
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await event.answer(
                notification="❌ You are not authorized. Contact the administrator."
            )
            return

        if not user.is_active:
            await event.answer(
                notification="❌ Your access has been revoked. Contact the administrator."
            )
            return

        # Update model
        old_model = user.selected_model
        user.selected_model = model_name

    # Update the message and drop the keyboard
    await event.edit(
        text=(
            f"✅ Model switched successfully!\n\n"
            f"Previous: {old_model or 'default'}\n"
            f"Current: {model_name}\n\n"
            f"You can now start chatting with the new model."
        ),
        attachments=[],
    )

    logger.info(
        "User selected model via keyboard",
        user_id=user_id,
        old_model=old_model,
        new_model=model_name
    )


@dp.message_created(Command("model_info"))
@authorized_only
async def model_info(event: MessageCreated, args: list[str] | None = None) -> None:
    """Show detailed information about a model."""
    if args:
        model_name = args[0]
    else:
        # Show info about current model
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.user_id == event_user_id(event))
            )
            user = result.scalar_one_or_none()
            model_name = user.selected_model if user else None

        if not model_name:
            await answer(
                event,
                "You haven't selected a model yet.\n"
                "Use /models to select one, or use:\n"
                "/model_info <model_name>"
            )
            return

    try:
        # Get model information
        info = await ollama_client.show_model_info(model_name)

        # Format the information
        message = f"🤖 <b>Model: {model_name}</b>\n\n"

        # Add model details if available
        if "details" in info:
            details = info["details"]
            if "parameter_size" in details:
                message += f"📊 Parameters: {details['parameter_size']}\n"
            if "quantization_level" in details:
                message += f"🔧 Quantization: {details['quantization_level']}\n"
            if "family" in details:
                message += f"👪 Family: {details['family']}\n"

        # Add license info if available
        if "license" in info:
            message += f"\n📜 License: {escape(str(info['license']))}\n"

        # Add template if available (truncated, escaped: it contains markup)
        template = info.get("template")
        if template:
            preview = template[:200] + "..." if len(template) > 200 else template
            message += f"\n📝 Template preview:\n<code>{escape(preview)}</code>\n"

        await answer_html(event, message)

    except Exception:
        logger.exception("Failed to get model info", model=model_name)
        await answer(
            event,
            f"❌ Failed to get information for model '{model_name}'.\n"
            "Make sure the model exists and try again."
        )


@dp.message_created(Command("current_model"))
@authorized_only
async def current_model(event: MessageCreated) -> None:
    """Show the currently selected model."""
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == event_user_id(event))
        )
        user = result.scalar_one_or_none()

    if user and user.selected_model:
        await answer_html(
            event,
            f"🤖 Current model: <b>{user.selected_model}</b>\n\n"
            "Use /models to switch to a different model."
        )
    else:
        await answer_html(
            event,
            f"🤖 Using default model: <b>{settings.DEFAULT_MODEL}</b>\n\n"
            "Use /models to select a different model."
        )
