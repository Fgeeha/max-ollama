"""Chat handler for conversations with Ollama models."""
import asyncio
import base64
import time
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
from bot.decorators import addressed_only, authorized_only, rate_limited
from bot.runtime import bot, dp, ollama_client
from bot.utils.context import ConversationContext, normalize_chat_messages
from bot.utils.events import answer, answer_html, event_chat_id, event_user_id
from bot.utils.ollama import OllamaModelNotFoundError, OllamaTimeoutError
from bot.utils.text import MAX_MESSAGE_CHARS, split_message
from bot.utils.time import utc_now

logger = structlog.get_logger()

IMAGE_PLACEHOLDER_PREFIX = "[image]"

# Streaming throttle: MAX rate-limits message edits, so grow the visible text
# in reasonably large steps and never faster than once per interval.
STREAM_FIRST_CHUNK_CHARS = 50
STREAM_EDIT_STEP_CHARS = 100
STREAM_EDIT_MIN_INTERVAL = 1.0

# MAX clears the typing indicator after a few seconds of silence.
TYPING_REFRESH_INTERVAL = 4.0

# A system prompt competes with the conversation for the context window.
MAX_SYSTEM_PROMPT_CHARS = 2000

DEFAULT_SYSTEM_PROMPT = (
    "Отвечай на том же языке, что и последнее сообщение пользователя."
)

# Updates are now handled concurrently, so one user can fire off several
# messages at once. Two generations for the same user would interleave their
# writes and scramble the stored conversation order, so only one runs at a time.
# The running task is kept so /stop can cancel it.
_generating: dict[int, asyncio.Task] = {}


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

    if user_id in _generating:
        await answer(
            event,
            "⏳ Я ещё отвечаю на предыдущее сообщение. Дождитесь ответа."
        )
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
        system_prompt = user.system_prompt or DEFAULT_SYSTEM_PROMPT

    if chat_id is not None:
        await bot.send_action(chat_id=chat_id, action=sender_action)

    if requires_image_support and not await ollama_client.supports_images(model_name):
        await answer(
            event,
            "⚠️ Текущая модель не умеет работать с изображениями.\n"
            "Используйте /models и выберите модель с поддержкой vision (например, llava)."
        )
        return

    _generating[user_id] = asyncio.current_task()
    try:
        # Get conversation context
        conv_context = ConversationContext(
            user_id,
            model_name,
            settings.MAX_CONTEXT_TOKENS,
        )
        messages = await conv_context.get_context()
        messages = normalize_chat_messages(messages)
        messages = [_mark_past_images(m) for m in messages]
        messages.insert(0, {"role": "system", "content": system_prompt})

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

        # Generate response
        start_time = time.time()

        # The typing indicator expires after a few seconds; refresh it for as
        # long as the model keeps working.
        typing = asyncio.create_task(_keep_typing(chat_id, sender_action))

        # Stream response for better UX
        response_text = ""
        bot_message_id: str | None = None
        last_edit_len = 0
        last_edit_time = 0.0

        async for chunk in ollama_client.chat_stream(model_name, messages):
            if chunk.get("message", {}).get("content"):
                response_text += chunk["message"]["content"]

                now = time.time()
                # Once the answer outgrows a single message, stop live-editing:
                # the final text is delivered as several messages below.
                if len(response_text) > MAX_MESSAGE_CHARS:
                    continue

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
                    except Exception as err:
                        # A failed intermediate edit is not fatal — the final
                        # text is sent below — but it must not vanish silently.
                        logger.warning("Streaming edit failed", error=str(err))

            if not chunk.get("done"):
                continue

            response_time_ms = chunk.get(
                "response_time_ms", int((time.time() - start_time) * 1000)
            )
            # Ollama reports token counts in the final chunk.
            prompt_tokens = chunk.get("prompt_eval_count") or 0
            completion_tokens = chunk.get("eval_count") or 0
            total_tokens = prompt_tokens + completion_tokens

            if not response_text.strip():
                await answer(event, "❌ Модель вернула пустой ответ. Попробуйте ещё раз.")
                return

            # Final delivery. Long answers exceed the messenger limit, so send
            # them as a sequence of messages instead of losing the tail.
            await _deliver(event, response_text, bot_message_id)

            # Save assistant response and usage stats
            async with get_session() as session:
                session.add(Conversation(
                    user_id=user_id,
                    model_name=model_name,
                    message_role="assistant",
                    message_content=response_text,
                    response_time_ms=response_time_ms,
                    tokens_used=total_tokens or None,
                ))

                # Update usage stats
                today = utc_now().date()

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
                    usage.total_tokens += total_tokens
                else:
                    session.add(ModelUsage(
                        user_id=user_id,
                        model_name=model_name,
                        request_count=1,
                        total_response_time_ms=response_time_ms,
                        total_tokens=total_tokens,
                        date=today,
                    ))


            # Update context
            await conv_context.add_message("assistant", response_text)

            logger.info(
                "Chat response generated",
                user_id=user_id,
                model=model_name,
                response_time_ms=response_time_ms,
                response_length=len(response_text),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

    except OllamaModelNotFoundError:
        await answer(
            event,
            f"❌ Model '{model_name}' not found.\n"
            "Please use /models to select an available model."
        )
    except asyncio.CancelledError:
        # /stop cancelled us. Awaiting anything here would just be cancelled
        # again, so the confirmation is sent by the /stop handler itself.
        logger.info("Generation cancelled by user", user_id=user_id)
        raise
    except (OllamaTimeoutError, TimeoutError):
        logger.warning("Generation timed out", user_id=user_id, model=model_name)
        await answer(
            event,
            "⏱️ Модель слишком долго не отвечает. Попробуйте ещё раз или выберите"
            " модель полегче через /models."
        )
    except Exception:
        logger.exception("Chat error", user_id=user_id, model=model_name)
        await answer(
            event,
            "❌ An error occurred while generating the response.\n"
            "Please try again later or contact the administrator."
        )
    finally:
        typing.cancel()
        _generating.pop(user_id, None)


def _sended_message_id(sended: Any) -> str | None:
    """Extract the message id from a SendMessage result, if any."""
    message = getattr(sended, "message", None)
    body = getattr(message, "body", None)
    return getattr(body, "mid", None)


async def _keep_typing(chat_id: int | None, action: SenderAction) -> None:
    """Re-send the chat action until cancelled."""
    if chat_id is None:
        return
    try:
        while True:
            await asyncio.sleep(TYPING_REFRESH_INTERVAL)
            await bot.send_action(chat_id=chat_id, action=action)
    except asyncio.CancelledError:
        pass
    except Exception as err:
        # Losing the indicator is cosmetic; never let it break the answer.
        logger.debug("Could not refresh typing indicator", error=str(err))


def _mark_past_images(message: dict[str, str]) -> dict[str, str]:
    """Make it explicit that an image from an earlier turn is not attached.

    History stores images as a text placeholder, so without this the model sees
    a caption referring to a picture it was never given.
    """
    content = message["content"]
    if message["role"] != "user" or not content.startswith(IMAGE_PLACEHOLDER_PREFIX):
        return message

    caption = content[len(IMAGE_PLACEHOLDER_PREFIX):].strip()
    return {
        "role": message["role"],
        "content": (
            f"[ранее пользователь присылал изображение; оно недоступно в этом "
            f"запросе] {caption}"
        ),
    }


async def _deliver(
    event: MessageCreated, text: str, placeholder_id: str | None
) -> None:
    """Send the finished answer, splitting it if it exceeds the message limit.

    ``placeholder_id`` is the streaming message already on screen: its content
    is replaced by the first piece, the rest follow as new messages.
    """
    pieces = split_message(text)

    for index, piece in enumerate(pieces):
        if index == 0 and placeholder_id is not None:
            try:
                await bot.edit_message(message_id=placeholder_id, text=piece)
                continue
            except Exception as err:
                # Editing failed (rate limit, message gone) — fall back to a
                # plain send so the user still gets the answer.
                logger.warning("Final edit failed, sending instead", error=str(err))

        await answer(event, piece)


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


@dp.message_created(Command("system"))
@authorized_only
async def system_prompt_command(
    event: MessageCreated, args: list[str] | None = None
) -> None:
    """Show, set or reset the caller's system prompt."""
    user_id = event_user_id(event)
    argument = " ".join(args).strip() if args else ""

    async with get_session() as session:
        user = await session.scalar(select(User).where(User.user_id == user_id))
        if not user:
            await answer(event, "❌ Пользователь не найден.")
            return

        if not argument:
            current = user.system_prompt
            if current:
                await answer_html(
                    event,
                    "🧭 <b>Ваш системный промпт:</b>\n\n"
                    f"<code>{escape(current)}</code>\n\n"
                    "Изменить: /system текст\n"
                    "Вернуть стандартный: /system reset"
                )
            else:
                await answer_html(
                    event,
                    "🧭 Используется стандартный системный промпт:\n\n"
                    f"<code>{escape(DEFAULT_SYSTEM_PROMPT)}</code>\n\n"
                    "Задать свой: /system текст"
                )
            return

        if argument.lower() in ("reset", "сброс", "default"):
            user.system_prompt = None
            await answer(event, "🧭 Восстановлен стандартный системный промпт.")
            logger.info("System prompt reset", user_id=user_id)
            return

        if len(argument) > MAX_SYSTEM_PROMPT_CHARS:
            await answer(
                event,
                f"❌ Слишком длинный промпт: {len(argument)} символов, "
                f"максимум {MAX_SYSTEM_PROMPT_CHARS}."
            )
            return

        user.system_prompt = argument

    await answer_html(
        event,
        f"🧭 Системный промпт обновлён:\n\n<code>{escape(argument)}</code>"
    )
    logger.info("System prompt updated", user_id=user_id, length=len(argument))


@dp.message_created(Command("stop"))
@authorized_only
async def stop_generation(event: MessageCreated) -> None:
    """Cancel the caller's running generation, if any."""
    user_id = event_user_id(event)
    task = _generating.get(user_id)

    if task is None or task.done():
        await answer(event, "Сейчас нечего останавливать.")
        return

    task.cancel()
    await answer(event, "🛑 Генерация остановлена.")


@dp.message_created(F.message.body.attachments)
@addressed_only
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
@addressed_only
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
