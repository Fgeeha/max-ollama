"""Chat handler for conversations with Ollama models."""
import base64
import time
from datetime import datetime
from html import escape
from typing import Any

import structlog
from maxapi import F
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.sender_action import SenderAction
from maxapi.filters.command import Command
from maxapi.types.updates.message_created import MessageCreated
from sqlalchemy import select

from bot.config import settings
from bot.database import Conversation, ModelUsage, User, get_session
from bot.decorators import authorized_only, rate_limited
from bot.runtime import bot, dp, ollama_client
from bot.utils.context import ConversationContext, normalize_chat_messages
from bot.utils.events import answer, answer_html, event_chat_id, event_user_id
from bot.utils.ollama import OllamaModelNotFoundError

logger = structlog.get_logger()

IMAGE_PLACEHOLDER_PREFIX = "[image]"

# Streaming throttle: MAX rate-limits message edits, so grow the visible text
# in reasonably large steps and never faster than once per interval.
STREAM_FIRST_CHUNK_CHARS = 50
STREAM_EDIT_STEP_CHARS = 100
STREAM_EDIT_MIN_INTERVAL = 1.0


async def _process_chat_interaction(
    event: MessageCreated,
    *,
    stored_user_message: str,
    payload_content: Any,
    sender_action: SenderAction = SenderAction.TYPING_ON,
    requires_image_support: bool = False,
    payload_images: list[str] | None = None,
) -> None:
    """Shared logic for sending a chat request to Ollama."""
    user_id = event_user_id(event)
    chat_id = event_chat_id(event)
    user_text = stored_user_message.strip()

    if not user_text:
        await answer(event, "❌ Cannot send an empty message.")
        return

    # Get user's selected model
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await answer(
                event,
                "❌ You are not authorized. Please contact the administrator."
            )
            return

        model_name = user.selected_model or settings.DEFAULT_MODEL

    if chat_id is not None:
        await bot.send_action(chat_id=chat_id, action=sender_action)

    if requires_image_support and not await ollama_client.supports_images(model_name):
        await answer(
            event,
            "⚠️ Текущая модель не умеет работать с изображениями.\n"
            "Используйте /models и выберите модель с поддержкой vision (например, llava)."
        )
        return

    try:
        # Get conversation context
        conv_context = ConversationContext(
            user_id,
            model_name,
            settings.MAX_CONTEXT_LENGTH,
        )
        messages = await conv_context.get_context()
        messages = normalize_chat_messages(messages)
        messages.insert(0, {
            "role": "system",
            "content": (
                "Отвечай на том же языке, что и последнее сообщение пользователя."
            ),
        })

        user_message = {"role": "user", "content": payload_content}
        if payload_images:
            user_message["images"] = payload_images
        messages.append(user_message)
        await conv_context.add_message("user", stored_user_message)

        # Save user message to database
        async with get_session() as session:
            session.add(Conversation(
                user_id=user_id,
                model_name=model_name,
                message_role="user",
                message_content=stored_user_message,
            ))
            await session.commit()

        # Generate response
        start_time = time.time()

        # Stream response for better UX
        response_text = ""
        bot_message_id: str | None = None
        last_edit_len = 0
        last_edit_time = 0.0

        async for chunk in ollama_client.chat_stream(model_name, messages):
            if chunk.get("message", {}).get("content"):
                response_text += chunk["message"]["content"]

                now = time.time()
                if bot_message_id is None:
                    if len(response_text) > STREAM_FIRST_CHUNK_CHARS:
                        sended = await answer(event, response_text + "...")
                        bot_message_id = _sended_message_id(sended)
                        last_edit_len = len(response_text)
                        last_edit_time = now
                elif (
                    len(response_text) - last_edit_len >= STREAM_EDIT_STEP_CHARS
                    and now - last_edit_time >= STREAM_EDIT_MIN_INTERVAL
                ):
                    last_edit_len = len(response_text)
                    last_edit_time = now
                    try:
                        await bot.edit_message(
                            message_id=bot_message_id,
                            text=response_text + "...",
                        )
                    except Exception:
                        pass

            if not chunk.get("done"):
                continue

            response_time_ms = chunk.get(
                "response_time_ms", int((time.time() - start_time) * 1000)
            )

            if not response_text.strip():
                await answer(event, "❌ Модель вернула пустой ответ. Попробуйте ещё раз.")
                return

            # Final update
            if bot_message_id is not None:
                try:
                    await bot.edit_message(
                        message_id=bot_message_id, text=response_text
                    )
                except Exception:
                    pass
            else:
                await answer(event, response_text)

            # Save assistant response and usage stats
            async with get_session() as session:
                session.add(Conversation(
                    user_id=user_id,
                    model_name=model_name,
                    message_role="assistant",
                    message_content=response_text,
                    response_time_ms=response_time_ms,
                ))

                # Update usage stats
                today = datetime.utcnow().date()

                result = await session.execute(
                    select(ModelUsage).where(
                        (ModelUsage.user_id == user_id) &
                        (ModelUsage.model_name == model_name) &
                        (ModelUsage.date == today)
                    )
                )
                usage = result.scalar_one_or_none()

                if usage:
                    usage.request_count += 1
                    usage.total_response_time_ms += response_time_ms
                else:
                    session.add(ModelUsage(
                        user_id=user_id,
                        model_name=model_name,
                        request_count=1,
                        total_response_time_ms=response_time_ms,
                        date=today,
                    ))

                await session.commit()

            # Update context
            await conv_context.add_message("assistant", response_text)

            logger.info(
                "Chat response generated",
                user_id=user_id,
                model=model_name,
                response_time_ms=response_time_ms,
                response_length=len(response_text),
            )

    except OllamaModelNotFoundError:
        await answer(
            event,
            f"❌ Model '{model_name}' not found.\n"
            "Please use /models to select an available model."
        )
    except TimeoutError:
        await answer(
            event,
            "⏱️ Request timed out. Please try again with a shorter message or different model."
        )
    except Exception as e:
        logger.error("Chat error", user_id=user_id, model=model_name, error=str(e))
        await answer(
            event,
            "❌ An error occurred while generating the response.\n"
            "Please try again later or contact the administrator."
        )


def _sended_message_id(sended: Any) -> str | None:
    """Extract the message id from a SendMessage result, if any."""
    message = getattr(sended, "message", None)
    body = getattr(message, "body", None)
    return getattr(body, "mid", None)


def _first_image_url(event: MessageCreated) -> str | None:
    """Return the download URL of the first image attachment, if any."""
    body = event.message.body
    for attachment in (body.attachments if body else None) or []:
        if attachment.type != AttachmentType.IMAGE:
            continue
        url = getattr(attachment.payload, "url", None)
        if url:
            return url
    return None


@dp.message_created(Command("clear"))
@authorized_only
async def clear_context(event: MessageCreated) -> None:
    """Start a fresh conversation for the user."""
    user_id = event_user_id(event)

    await ConversationContext(user_id, "").reset()

    await answer(
        event,
        "🧹 Conversation context cleared!\n"
        "You can start a fresh conversation now."
    )


@dp.message_created(Command("regenerate"))
@authorized_only
@rate_limited
async def regenerate_response(event: MessageCreated) -> None:
    """Regenerate the last assistant response."""
    user_id = event_user_id(event)

    # Get last user message
    async with get_session() as session:
        result = await session.execute(
            select(Conversation)
            .where(
                (Conversation.user_id == user_id) &
                (Conversation.message_role == "user")
            )
            .order_by(Conversation.id.desc())
            .limit(1)
        )
        last_user_msg = result.scalar_one_or_none()

        if not last_user_msg:
            await answer(event, "❌ No previous message found to regenerate.")
            return

        if not last_user_msg.message_content or not last_user_msg.message_content.strip():
            await answer(event, "❌ Last message was empty, cannot regenerate.")
            return

        if last_user_msg.message_content.lower().startswith(IMAGE_PLACEHOLDER_PREFIX):
            await answer(
                event,
                "♻️ Нельзя повторно сгенерировать ответ для сообщения с изображением."
            )
            return

        # Delete last assistant response if exists
        result = await session.execute(
            select(Conversation)
            .where(
                (Conversation.user_id == user_id) &
                (Conversation.message_role == "assistant") &
                (Conversation.id > last_user_msg.id)
            )
            .order_by(Conversation.id.desc())
            .limit(1)
        )
        last_assistant_msg = result.scalar_one_or_none()

        if last_assistant_msg:
            await session.delete(last_assistant_msg)
            await session.commit()

    await answer(event, "🔄 Regenerating response...")

    await _process_chat_interaction(
        event,
        stored_user_message=last_user_msg.message_content,
        payload_content=last_user_msg.message_content,
    )


@dp.message_created(Command("history"))
@authorized_only
async def show_history(event: MessageCreated) -> None:
    """Show recent conversation history."""
    user_id = event_user_id(event)

    async with get_session() as session:
        result = await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc())
            .limit(10)
        )
        messages = result.scalars().all()

    if not messages:
        await answer(event, "No conversation history found.")
        return

    # Reverse to show chronological order
    history = "📜 <b>Recent Conversation History:</b>\n\n"
    for msg in reversed(messages):
        role_emoji = "👤" if msg.message_role == "user" else "🤖"
        # Truncate long messages
        content = msg.message_content
        content = content[:200] + "..." if len(content) > 200 else content
        time_str = msg.created_at.strftime("%H:%M")

        history += (
            f"{role_emoji} <b>{msg.message_role.title()} ({time_str}):</b>\n"
            f"{escape(content)}\n\n"
        )

    await answer_html(event, history)


@dp.message_created(F.message.body.attachments)
@authorized_only
@rate_limited
async def handle_photo(event: MessageCreated) -> None:
    """Handle image messages by forwarding them to a vision-capable model."""
    image_url = _first_image_url(event)
    if not image_url:
        await answer(event, "❌ Я умею обрабатывать только изображения.")
        return

    try:
        image_bytes = await bot.download_bytes(image_url)
    except Exception as err:
        logger.error("Failed to download photo", error=str(err))
        await answer(event, "❌ Не удалось загрузить изображение. Попробуйте ещё раз.")
        return

    body = event.message.body
    caption = (body.text or "").strip() if body else ""
    caption = caption or "Опиши это изображение."

    await _process_chat_interaction(
        event,
        stored_user_message=f"{IMAGE_PLACEHOLDER_PREFIX} {caption}",
        payload_content=caption,
        payload_images=[base64.b64encode(image_bytes).decode("utf-8")],
        sender_action=SenderAction.SENDING_PHOTO,
        requires_image_support=True,
    )


@dp.message_created(F.message.body.text)
@authorized_only
@rate_limited
async def handle_message(event: MessageCreated) -> None:
    """Handle regular chat messages (registered last: catches everything else)."""
    body = event.message.body
    message_text = (body.text if body else "") or ""

    if message_text.startswith("/"):
        await answer(
            event,
            "❓ Unknown command. Use /help to see the available commands."
        )
        return

    await _process_chat_interaction(
        event,
        stored_user_message=message_text,
        payload_content=message_text,
    )
