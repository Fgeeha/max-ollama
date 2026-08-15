"""Admin command handlers."""
from datetime import datetime, timedelta
from html import escape

import structlog
from maxapi.enums.parse_mode import ParseMode
from maxapi.filters.command import Command
from maxapi.types.updates.message_created import MessageCreated
from sqlalchemy import func, select

from bot.config import settings
from bot.database import Conversation, ModelUsage, User, get_session
from bot.decorators import admin_only
from bot.runtime import bot, dp, ollama_client
from bot.utils.context import ConversationContext
from bot.utils.events import answer, answer_html, event_user_id
from bot.utils.runtime_settings import TEST_MODE_KEY, set_flag

logger = structlog.get_logger()


@dp.message_created(Command("add_user"))
@admin_only
async def add_user(event: MessageCreated, args: list[str] | None = None) -> None:
    """Add a new authorized user."""
    if not args:
        await answer(
            event,
            "Usage: /add_user <user_id>\n"
            "Example: /add_user 123456789"
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await answer(event, "❌ Invalid user ID. Please provide a numeric ID.")
        return

    async with get_session() as session:
        # Check if user already exists
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        existing_user = result.scalar_one_or_none()

        if existing_user:
            if existing_user.is_active:
                await answer(event, f"ℹ️ User {user_id} is already authorized.")
            else:
                existing_user.is_active = True
                await session.commit()
                await answer(event, f"✅ User {user_id} has been reactivated.")
        else:
            # Add new user
            new_user = User(
                user_id=user_id,
                full_name=f"User {user_id}",
                is_active=True,
                is_admin=False
            )
            session.add(new_user)
            await session.commit()
            await answer(event, f"✅ User {user_id} has been authorized.")

            logger.info("User added by admin", user_id=user_id, admin_id=event_user_id(event))


@dp.message_created(Command("remove_user"))
@admin_only
async def remove_user(event: MessageCreated, args: list[str] | None = None) -> None:
    """Remove/deactivate a user."""
    if not args:
        await answer(
            event,
            "Usage: /remove_user <user_id>\n"
            "Example: /remove_user 123456789"
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await answer(event, "❌ Invalid user ID. Please provide a numeric ID.")
        return

    # Prevent admin from removing themselves
    if user_id == event_user_id(event):
        await answer(event, "❌ You cannot remove yourself!")
        return

    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await answer(event, f"❌ User {user_id} not found.")
        elif not user.is_active:
            await answer(event, f"ℹ️ User {user_id} is already deactivated.")
        else:
            user.is_active = False
            await session.commit()
            await answer(event, f"✅ User {user_id} has been deactivated.")

            logger.info("User removed by admin", user_id=user_id, admin_id=event_user_id(event))


@dp.message_created(Command("list_users"))
@admin_only
async def list_users(event: MessageCreated) -> None:
    """List all authorized users."""
    async with get_session() as session:
        result = await session.execute(
            select(User).order_by(User.created_at.desc())
        )
        users = result.scalars().all()

    if not users:
        await answer(event, "No users found.")
        return

    message = "👥 <b>Authorized Users:</b>\n\n"

    for user in users:
        status = "✅" if user.is_active else "❌"
        admin_badge = "👑" if user.is_admin else ""
        username = f"@{escape(user.username)}" if user.username else "No username"

        message += (
            f"{status} <b>{user.user_id}</b> {admin_badge}\n"
            f"   Name: {escape(user.full_name)}\n"
            f"   Username: {username}\n"
            f"   Model: {escape(user.selected_model or 'default')}\n"
            f"   Added: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        )

    await answer_html(event, message)


@dp.message_created(Command("test_mode"))
@admin_only
async def toggle_test_mode(event: MessageCreated, args: list[str] | None = None) -> None:
    """Toggle test mode on/off."""
    if not args:
        # Show current status
        status = "ON 🔒" if settings.TEST_MODE else "OFF 🔓"
        await answer_html(
            event,
            f"Test mode is currently: <b>{status}</b>\n\n"
            "Use /test_mode on or /test_mode off to change."
        )
        return

    command = args[0].lower()
    if command not in ["on", "off"]:
        await answer(event, "❌ Use /test_mode on or /test_mode off")
        return

    new_state = command == "on"

    # Persist first, then apply: a restart must keep the admin's choice.
    await set_flag(TEST_MODE_KEY, new_state)
    settings.TEST_MODE = new_state

    status = "ON 🔒 (Admin only)" if new_state else "OFF 🔓 (All users)"
    await answer_html(event, f"✅ Test mode is now: <b>{status}</b>")

    logger.info("Test mode toggled", new_state=new_state, admin_id=event_user_id(event))


@dp.message_created(Command("stats"))
@admin_only
async def show_stats(event: MessageCreated) -> None:
    """Show usage statistics."""
    async with get_session() as session:
        # Get user count
        user_count = await session.execute(
            select(func.count(User.user_id)).where(User.is_active.is_(True))
        )
        active_users = user_count.scalar()

        # Get conversation stats for last 24 hours
        yesterday = datetime.utcnow() - timedelta(days=1)
        since_day = yesterday.date()
        conv_stats = await session.execute(
            select(
                func.count(Conversation.id).label("total"),
                func.count(func.distinct(Conversation.user_id)).label("unique_users")
            ).where(Conversation.created_at >= yesterday)
        )
        stats = conv_stats.one()

        # Get model usage stats
        model_stats = await session.execute(
            select(
                ModelUsage.model_name,
                func.sum(ModelUsage.request_count).label("requests"),
                func.sum(ModelUsage.total_tokens).label("tokens"),
                func.avg(ModelUsage.total_response_time_ms / ModelUsage.request_count).label("avg_time")
            )
            .where(ModelUsage.date >= since_day)
            .group_by(ModelUsage.model_name)
            .order_by(func.sum(ModelUsage.request_count).desc())
        )
        model_data = model_stats.all()

    message = (
        "📊 <b>Bot Statistics (Last 24h)</b>\n\n"
        f"👥 Active Users: {active_users}\n"
        f"💬 Total Messages: {stats.total}\n"
        f"🔄 Unique Users: {stats.unique_users}\n\n"
    )

    if model_data:
        message += "<b>Model Usage:</b>\n"
        for model in model_data:
            avg_time = model.avg_time or 0
            line = f"• {model.model_name}: {model.requests} requests (avg {avg_time:.0f}ms)"
            if model.tokens:
                line += f", {model.tokens} tokens"
            message += line + "\n"

    # Get Ollama status
    if await ollama_client.health_check():
        models = await ollama_client.get_model_names()
        message += f"\n✅ Ollama: Online ({len(models)} models available)"
    else:
        message += "\n❌ Ollama: Offline"

    await answer_html(event, message)


@dp.message_created(Command("clear_history"))
@admin_only
async def clear_history(event: MessageCreated, args: list[str] | None = None) -> None:
    """Clear conversation history for a user or all users."""
    if not args:
        await answer(
            event,
            "Usage:\n"
            "/clear_history <user_id> - Clear history for specific user\n"
            "/clear_history all - Clear all history\n"
        )
        return

    target = args[0]

    async with get_session() as session:
        if target.lower() == "all":
            # Clear all history
            await session.execute(Conversation.__table__.delete())
            await session.commit()
            # Deleted rows are still cached in memory and would keep reaching
            # the model until a restart.
            ConversationContext.clear_all()
            await answer(event, "✅ All conversation history has been cleared.")
            logger.info("All conversation history cleared", admin_id=event_user_id(event))
            return

        try:
            user_id = int(target)
        except ValueError:
            await answer(event, "❌ Invalid user ID. Use a number or 'all'.")
            return

        result = await session.execute(
            Conversation.__table__.delete().where(Conversation.user_id == user_id)
        )
        await session.commit()

    ConversationContext.forget(user_id)

    if result.rowcount > 0:
        await answer(event, f"✅ Cleared {result.rowcount} messages for user {user_id}.")
        logger.info(
            "User history cleared",
            user_id=user_id,
            admin_id=event_user_id(event),
            messages_deleted=result.rowcount
        )
    else:
        await answer(event, f"ℹ️ No history found for user {user_id}.")


@dp.message_created(Command("broadcast"))
@admin_only
async def broadcast(event: MessageCreated, args: list[str] | None = None) -> None:
    """Broadcast a message to all active users."""
    if not args:
        await answer(
            event,
            "Usage: /broadcast <message>\n"
            "Example: /broadcast The bot will be down for maintenance at 10 PM UTC"
        )
        return

    message = " ".join(args)

    async with get_session() as session:
        result = await session.execute(
            select(User.user_id).where(User.is_active.is_(True))
        )
        user_ids = [row[0] for row in result]

    if not user_ids:
        await answer(event, "No active users to broadcast to.")
        return

    success = 0
    failed = 0

    broadcast_msg = f"📢 <b>Admin Broadcast:</b>\n\n{message}"

    for user_id in user_ids:
        try:
            await bot.send_message(
                user_id=user_id,
                text=broadcast_msg,
                format=ParseMode.HTML,
            )
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to send broadcast to {user_id}", error=str(e))

    await answer(
        event,
        f"✅ Broadcast complete!\n"
        f"Success: {success}\n"
        f"Failed: {failed}"
    )

    logger.info(
        "Broadcast sent",
        admin_id=event_user_id(event),
        success=success,
        failed=failed
    )
